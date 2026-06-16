"""Command-center (assistant) checks. Run: python3 test_assistant.py

Proves the conversational control surface: read tools answer directly, the connect
tool hands back a link, saving a contact writes the directory, and a text-a-lead is
GATED behind an explicit confirm (never auto-sent) yet still flows through the gated
messaging seam. Throwaway temp DB + the deterministic demo brain; no network.
"""
import os
import tempfile

os.environ["RINGBACK_PROVIDER"] = "demo"          # deterministic, no network
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""                  # configured() False -> sends simulate

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


# Seed a lead so list/text tools have something to act on.
lead_id = db.create_lead(1, "Dana Homeowner", "+15551234567")
biz = db.get_business(1)

# --- the command center is the signed-in home ---
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
r = client.get("/dashboard")
html = r.get_data(as_text=True)
check("home is the command center surface",
      r.status_code == 200 and "command-shell" in html and "commandInput" in html)
check("command center loads the orb + assistant assets",
      "assistant.js" in html and 'id="orb"' in html)
pipe = client.get("/pipeline")
check("pipeline still renders the cockpit",
      pipe.status_code == 200 and "Leads captured" in pipe.get_data(as_text=True)
      and "command-shell" not in pipe.get_data(as_text=True))

# --- read tools answer directly ---
out = assistant.run(biz, "how many leads came in this week?")
check("stats command returns a stat card",
      out["pending_action"] is None and any(c["type"] == "stat" for c in out["cards"]))
out = assistant.run(biz, "show my booked estimates")
check("appointments command answers (list or honest empty)",
      out["pending_action"] is None)
out = assistant.run(biz, "connect my google calendar")
check("connect returns a link to settings",
      any(c.get("type") == "link" and c.get("href") == "/settings" for c in out["cards"]))

# --- saving a contact writes the directory ---
out = assistant.run(biz, "save 555 867 5309 as a customer")
check("save-contact stores a directory entry",
      out["pending_action"] is None and db.get_contact(1, "5558675309") is not None)

# --- texting a lead is GATED behind a confirm ---
out = assistant.run(biz, "text my last lead saying running ten minutes late")
check("text-a-lead is gated behind a confirm (not auto-sent)",
      out["pending_action"] is not None and out["pending_action"]["tool"] == "text_lead"
      and out["cards"] == [])
# confirm actually runs it, through the (simulated) gated seam
res = assistant.execute(biz, "text_lead", out["pending_action"]["args"])
check("confirming the text runs the gated send",
      any(c.get("type") == "note" for c in res["cards"]))

# --- scheduling: the exact gap the screenshot exposed ---
out = assistant.run(biz, "can we customize my scheduling")
check("vague 'customize my scheduling' shows the prefs (no dead-end)",
      out["pending_action"] is None
      and any(c.get("title") == "Your scheduling" for c in out["cards"]))
out = assistant.run(biz, "I don't want a 2pm and a 3pm, I'd never make both that close")
check("a back-to-back complaint is routed to a buffer change (gated)",
      out["pending_action"] is not None
      and out["pending_action"]["tool"] == "set_scheduling"
      and out["pending_action"]["args"].get("buffer_minutes"))
res = assistant.execute(biz, "set_scheduling", out["pending_action"]["args"])
check("confirming sets a real buffer that blocks adjacent slots",
      db.scheduling_prefs(1)["buffer_minutes"] >= 90)
out = assistant.run(biz, "only book monday to friday")
check("working-days change is gated", out["pending_action"]
      and out["pending_action"]["args"].get("working_days") == [0, 1, 2, 3, 4])
assistant.execute(biz, "set_scheduling", out["pending_action"]["args"])
check("confirming sets working days", db.scheduling_prefs(1)["working_days"] == {0, 1, 2, 3, 4})
out = assistant.run(biz, "offer estimates at 10am and 3pm")
assistant.execute(biz, "set_scheduling", out["pending_action"]["args"])
check("setting estimate windows persists",
      {db.time_key(t) for t in db.scheduling_prefs(1)["times"]} == {"10:00", "15:00"})

# --- capability honesty: unsupported asks route to a real page, not a dead-end ---
out = assistant.run(biz, "how do I change my AI instructions")
check("a profile question routes to Settings (no dead-end)",
      out["pending_action"] is None and any(c.get("href") == "/settings" for c in out["cards"]))

# --- the HTTP routes return JSON ---
r = client.post("/assistant", data={"message": "how many leads this week?"})
check("/assistant route returns JSON", r.status_code == 200 and r.is_json)
r = client.post("/assistant/confirm",
                data={"tool": "text_lead",
                      "args": '{"message": "see you then", "name": "Dana"}'})
check("/assistant/confirm runs the gated action", r.status_code == 200 and r.is_json)

# --- command-center memory: record real questions, flag the weak spots, learn ---
import convos as _cv
client.post("/assistant", data={"message": "how many leads this week?", "convo_key": "memk1"})
check("the conversation is recorded with turns",
      any(cv["turns"] >= 2 for cv in db.list_convos(1)))
client.post("/assistant", data={"message": "how do I change my AI instructions",
                                "convo_key": "memk1"})
check("a capability gap is flagged", db.flag_counts(1).get("capability_gap", 0) >= 1)
client.post("/assistant", data={"message": "how do I change my AI instructions",
                                "convo_key": "memk1"})
check("a repeated ask is flagged", db.flag_counts(1).get("repeat", 0) >= 1)
_cv.teach(1, "pause the bot", "answer", "Paused. Say resume to turn it back on.")
_lr = assistant.run(biz, "please pause the bot for now")
check("a taught correction is honored on the next ask",
      _lr.get("meta", {}).get("status") == "learned" and "Paused" in _lr["reply"])
_cv.teach(1, "my numbers", "get_stats")
_lt = assistant.run(biz, "show me my numbers")
check("a taught tool mapping runs that tool",
      any(card["type"] == "stat" for card in _lt.get("cards", [])))
r = client.get("/training")
check("training page renders the memory surface",
      r.status_code == 200 and b"Memory" in r.data)
_fl = db.list_flags(1, resolved=0, limit=5)
if _fl:
    r = client.post("/training/teach",
                    data={"pattern": "show my pipeline", "action": "get_stats",
                          "flag_id": str(_fl[0]["id"])})
    check("teaching through the page adds a learning and resolves the flag",
          r.status_code == 302
          and any(l["pattern"] == "show my pipeline" for l in db.list_learnings(1))
          and db.get_flag(1, _fl[0]["id"])["resolved"] == 1)
_cvs = db.list_convos(1, limit=1)
if _cvs:
    check("a saved conversation can be replayed",
          client.get("/training/convo/%d" % _cvs[0]["id"]).status_code == 200)

# LLM grading: catch subtle misses the heuristics pass (brain verdict stubbed for determinism).
_grade_orig = _cv._grade
_gcv = db.start_or_get_convo(1, "gradekey")
_gtid = db.log_turn(_gcv, 1, "user", "what's my busiest day")
_cv._grade = lambda m, r: {"verdict": "miss", "reason": "Did not answer the real question."}
_b = db.flag_counts(1).get("unhelpful", 0)
_cv.grade_exchange(1, _gcv, _gtid, "what's my busiest day", "Here are your leads.", {"status": "ok"})
check("an LLM 'miss' verdict adds an unhelpful flag with the reason",
      db.flag_counts(1).get("unhelpful", 0) == _b + 1)
_cv._grade = lambda m, r: {"verdict": "good", "reason": "answered"}
_b = db.flag_counts(1).get("unhelpful", 0)
_cv.grade_exchange(1, _gcv, _gtid, "show my leads", "Here they are.", {"status": "ok"})
check("an LLM 'good' verdict adds no flag", db.flag_counts(1).get("unhelpful", 0) == _b)
_cv._grade = _grade_orig
check("the LLM-graded miss surfaces on the training page",
      b"missed the mark" in client.get("/training").data)

# Weekly digest + ranked "build these next" gaps (next-steps surfacing).
_dg = _cv.digest(1)
check("the digest summarizes recent activity",
      _dg["has_content"] and _dg["line"].startswith("This week"))
_tu = _cv.top_unmet(1)
check("top unmet ranks recurring gaps by frequency",
      isinstance(_tu, list) and (not _tu or _tu[0]["count"] >= 1))
check("the command center surfaces the digest line",
      b"convo-digest" in client.get("/dashboard").data)
check("the training page ranks what to build next",
      (not _tu) or b"Build these next" in client.get("/training").data)

print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
import sys
sys.exit(1 if _fail else 0)
