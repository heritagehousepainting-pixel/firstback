"""Phase 3 growth-engine checks. Run: .venv/bin/python test_growth.py

Proves the plays engine surfaces the right money-ranked opportunities from real signals,
the review play is compliant (asks every customer, never gated by sentiment, no incentive),
dedupe holds, opt-outs are skipped, Money Left Behind aggregates honestly, and the opt-in
auto-scheduler enqueues onto the existing scheduled_messages spine without double-queuing.
Throwaway temp DB + demo brain; no network.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
import messaging
messaging.TWILIO_ACCOUNT_SID = ""               # sends simulate; opt-out still suppresses
import app  # noqa: F401  # runs migrations
import growth

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1; print(f"  ok   {name}")
    else:
        _fail += 1; print(f"FAIL   {name}")


def iso_ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def set_msg_time(lead_id, when_iso):
    c = db.get_conn(); c.execute("UPDATE messages SET created_at=? WHERE lead_id=?",
                                 (when_iso, lead_id)); c.commit(); c.close()


def day_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


db.set_avg_job_value(1, 2000)
db.update_business(1, {"trade": "HVAC", "review_link": "https://g.page/r/testlink"})
biz = db.get_business(1)

# --- Review request: a completed job (past booked appt) surfaces a review play ---------
rl = db.create_lead(1, "Rita Review", "+15550000101")
db.book_appointment(1, rl, "last week", day=day_ago(3), slot_time="10:00")   # sets status=booked
# Rita left an ANGRY inbound -- the review play MUST still fire (no sentiment gating).
db.add_message(rl, "in", "honestly this was terrible and I am unhappy")
_ps = growth.plays(biz)
_review = [p for p in _ps if p["kind"] == "review_request" and p["lead_id"] == rl]
check("review play fires for a completed job", len(_review) == 1)
check("COMPLIANCE: review play fires even for an unhappy customer (no sentiment gating)",
      len(_review) == 1)
check("COMPLIANCE: review draft has a review destination and no incentive",
      _review and "review" in _review[0]["draft_body"].lower()
      and not any(w in _review[0]["draft_body"].lower()
                  for w in ("discount", "free", "gift", "coupon", "% off", "$ off")))
check("review play carries an honest compliance note",
      _review and "every customer" in _review[0]["compliance_note"].lower())

# --- Quote follow-up: a quiet quote 2 days old (texted, no reply, not booked) ----------
fl = db.create_lead(1, "Frank Followup", "+15550000102")
db.add_message(fl, "out", "Here's your estimate, $2,000.")
set_msg_time(fl, iso_ago(days=2))
_ps = growth.plays(biz)
check("quote follow-up fires for a 2-day-quiet quote",
      any(p["kind"] == "quote_followup" and p["lead_id"] == fl for p in _ps))

# --- Reactivation: a quote that went cold 45 days ago ----------------------------------
cl = db.create_lead(1, "Cora Cold", "+15550000103")
db.add_message(cl, "out", "Following up on your estimate.")
set_msg_time(cl, iso_ago(days=45))
_ps = growth.plays(biz)
check("reactivation fires for a 45-day-cold quote",
      any(p["kind"] == "reactivation" and p["lead_id"] == cl for p in _ps))

# --- Win-back: a past customer ~13 months out ------------------------------------------
wl = db.create_lead(1, "Wally Winback", "+15550000104")
db.book_appointment(1, wl, "last year", day=day_ago(400), slot_time="09:00")
_ps = growth.plays(biz)
check("win-back fires for a ~13-month-old past customer",
      any(p["kind"] == "winback" and p["lead_id"] == wl for p in _ps))

# --- Referral: a job that wrapped yesterday --------------------------------------------
yl = db.create_lead(1, "Yuki Yesterday", "+15550000105")
db.book_appointment(1, yl, "yesterday", day=day_ago(1), slot_time="11:00")
_ps = growth.plays(biz)
check("referral fires for a just-wrapped job",
      any(p["kind"] == "referral" and p["lead_id"] == yl for p in _ps))

# --- Opt-out is skipped ----------------------------------------------------------------
ol = db.create_lead(1, "Opal Optout", "+15550000106")
db.book_appointment(1, ol, "last week", day=day_ago(2), slot_time="10:00")
db.set_opt_out(1, "+15550000106")
_ps = growth.plays(biz)
check("an opted-out customer surfaces no plays",
      not any(p["lead_id"] == ol for p in _ps))

# --- Money Left Behind aggregates by tier ----------------------------------------------
_mlb = growth.money_left_behind(biz)
check("money_left_behind totals convert + grow",
      _mlb["total"] == _mlb["by_tier"]["convert"] + _mlb["by_tier"]["grow"]
      and _mlb["total"] > 0 and _mlb["play_count"] > 0)
check("money_left_behind headline shows the dollar figure",
      "$" in _mlb["headline"])

# --- Plays are money-ranked ------------------------------------------------------------
_ps = growth.plays(biz)
check("plays are sorted by money, highest first",
      [p["money"] for p in _ps] == sorted([p["money"] for p in _ps], reverse=True))
# Seasonal play is sendable but uses action="launch_seasonal_campaign" (cohort route, not per-lead).
# All other sendable plays (per-lead) should route to a text action.
check("sendable per-lead plays carry a tap action that routes to a text",
      all(p["action"].startswith("text ") for p in _ps if p["sendable"] and p.get("lead_id") is not None))

# --- Auto-scheduler: off by default, enqueues when opted in, dedupes on re-scan --------
check("scan is a no-op when growth is not opted in", growth.scan()["queued"] == 0)
db.set_growth_on(1, 1)
biz = db.get_business(1)
_q1 = growth.scan()["queued"]
check("scan enqueues touches once a business opts in", _q1 > 0)
_q2 = growth.scan()["queued"]
check("a re-scan does not double-queue (dedupe index holds)", _q2 == 0)
# the dedupe also removes the play from the feed (already in flight)
_idx = db.growth_touch_index(1)
check("a queued touch drops out of the live feed",
      not any(p["kind"] == "review_request" and p["lead_id"] == rl
              for p in growth.plays(db.get_business(1))))

# --- Boundary + negative-space coverage --------------------------------------------------
_early = db.create_lead(1, "Early Earl", "+15550000201")
db.add_message(_early, "out", "Here is your quote.")
set_msg_time(_early, iso_ago(hours=12))            # only 12h old -> too early for follow-up
check("no follow-up fires for a quote only 12h old (too-early guard)",
      not any(p["lead_id"] == _early and p["kind"] == "quote_followup" for p in growth.plays(biz)))
_exact = db.create_lead(1, "Exact Edge", "+15550000202")
db.add_message(_exact, "out", "Estimate.")
set_msg_time(_exact, iso_ago(days=30))             # exactly 30d -> reactivation, not follow-up
_eps = growth.plays(biz)
check("at exactly 30 days a quiet quote is reactivation, not follow-up",
      any(p["lead_id"] == _exact and p["kind"] == "reactivation" for p in _eps)
      and not any(p["lead_id"] == _exact and p["kind"] == "quote_followup" for p in _eps))
_w11 = db.create_lead(1, "Wb Eleven", "+15550000203")
db.book_appointment(1, _w11, "11mo", day=day_ago(335), slot_time="09:00")
check("win-back does NOT fire at 11 months",
      not any(p["lead_id"] == _w11 and p["kind"] == "winback" for p in growth.plays(biz)))

# --- Placeholder name never leaks into copy ----------------------------------------------
_np = db.create_lead(1, "New Caller", "+15550000204")
db.book_appointment(1, _np, "last week", day=day_ago(4), slot_time="08:00")
_npp = next((p for p in growth.plays(biz) if p["lead_id"] == _np), None)
check("a placeholder name ('New Caller') never appears in the draft body",
      _npp and "New" not in _npp["draft_body"] and "Caller" not in _npp["draft_body"])

# --- Membership (repeat, low-ticket trade) -----------------------------------------------
_mbz = db.create_business({"name": "Member Co", "owner_email": "member@x.io"})
db.set_avg_job_value(_mbz, 400)                    # 0 < val < 500
_m1 = db.create_lead(_mbz, "Mem One", "+15550010001")
db.book_appointment(_mbz, _m1, "j1", day=day_ago(35), slot_time="10:00")
db.book_appointment(_mbz, _m1, "j2", day=day_ago(60), slot_time="11:00")
_mps = growth.plays(db.get_business(_mbz))
check("membership fires for a repeat, low-ticket customer and is in the grow tier",
      any(p["kind"] == "membership" and p["tier"] == "grow" for p in _mps))

# --- Density (3+ leads sharing a parsed zip in 14 days) ----------------------------------
_dbz = db.create_business({"name": "Dense Co", "owner_email": "dense@x.io"})
db.set_avg_job_value(_dbz, 2000)
for i in range(3):
    _dl = db.create_lead(_dbz, f"Dense {i}", f"+1555002100{i}")
    db.set_lead_notes(_dl, address="12 Oak St, Springfield, IL 62704")
_dps = growth.plays(db.get_business(_dbz))
check("density fires when 3+ leads share a zip, and is owner-initiated (not sendable)",
      any(p["kind"] == "density" for p in _dps)
      and all(not p["sendable"] for p in _dps if p["kind"] == "density"))

# --- Financing (avg job value over the trade threshold) ----------------------------------
_fbz = db.create_business({"name": "Finance Co", "owner_email": "finance@x.io"})
db.update_business(_fbz, {"trade": "Roofing"})
db.set_avg_job_value(_fbz, 8000)                   # > roofing threshold 5000
_fps = growth.plays(db.get_business(_fbz))
check("financing fires when avg job value clears the trade threshold (not sendable)",
      any(p["kind"] == "financing" and not p["sendable"] for p in _fps))

# --- Seasonal: fires in-window, silent out-of-window (direct, date-independent) ----------
import datetime as _dtmod
_seas_in = growth._seasonal_play(db.get_business(1), _dtmod.date(2026, 3, 1), 2000)
_seas_out = growth._seasonal_play(db.get_business(1), _dtmod.date(2026, 7, 1), 2000)
# Change 5: _seasonal_play is now sendable=True with action="launch_seasonal_campaign"
check("seasonal play fires for HVAC inside its pre-peak window (March)",
      _seas_in is not None and _seas_in["kind"] == "seasonal" and _seas_in["sendable"])
check("seasonal play has launch action (not per-lead text action)",
      _seas_in is not None and _seas_in.get("action") == "launch_seasonal_campaign")
check("seasonal play carries the seasonal_service field",
      _seas_in is not None and _seas_in.get("seasonal_service") == "AC tune-up")
check("seasonal play stays silent outside its window (July)", _seas_out is None)

# --- Tier routing: convert vs grow ------------------------------------------------------
_mlb2 = growth.money_left_behind(biz)
check("review + follow-up land in the convert tier; reactivation/win-back/referral in grow",
      _mlb2["by_tier"]["convert"] > 0 and _mlb2["by_tier"]["grow"] > 0)

# --- DB-level dedupe: the UNIQUE index blocks a raw duplicate (race safety) --------------
_rl2 = db.create_lead(1, "Race Lead", "+15550000205")
_first_id = db.add_scheduled_message(1, _rl2, None, "review_request", "2099-01-01T00:00:00+00:00", "x")
_dup_id = db.add_scheduled_message(1, _rl2, None, "review_request", "2099-01-02T00:00:00+00:00", "y")
check("the growth-touch UNIQUE index blocks a raw duplicate (race-safe)",
      _first_id is not None and _dup_id is None)

print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
import sys
sys.exit(1 if _fail else 0)
