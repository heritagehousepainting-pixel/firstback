"""Phase 6c (code) -- W4-W7 integration edge cases. Standalone.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_phase6c.py

  W5 -- a FAILED growth touch frees its (lead, kind) slot so a later cycle can re-queue.
  W6 -- queuing a Touch-1 followup cancels the lead's PENDING quote_followup (held tray
        plays left untouched).
  W7 -- cancel_appointment cancels the appointment's pending reminders in the SAME
        transaction (no orphaned 'skipped' reminder), and still rejects cross-tenant ids.
  W4 -- tick_once timing/soft-budget warn is observability-only: inert on a fast tick.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
db.init_db()
import messaging
messaging.TWILIO_ACCOUNT_SID = ""           # configured() False -> simulates
import reminders

_pass = _fail = 0
def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1; print(f"  ok   {name}")
    else:
        _fail += 1; print(f"FAIL   {name}")

_SA = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

def _status(sid):
    c = db.get_conn()
    r = c.execute("SELECT status FROM scheduled_messages WHERE id=?", (sid,)).fetchone()
    c.close()
    return r[0] if r else None


# ===========================================================================
# W5 -- a failed growth touch can re-queue
# ===========================================================================
print("\n=== W5: failed growth touch frees its slot ===")
b5 = db.create_business({"name": "W5 Co"})
l5 = db.create_lead(b5, "W5 Lead", "+15550050001")
id1 = db.add_scheduled_message(b5, l5, None, "review_request", _SA, "review pls")
check("W5: first review_request queued", id1 is not None)
dup = db.add_scheduled_message(b5, l5, None, "review_request", _SA, "dup while active")
check("W5: a second ACTIVE review_request is blocked (slot held)", dup is None)
# While the touch is PENDING, growth_touch_index reports it as in-flight (so plays()/scan()
# won't re-offer it).
check("W5: a pending touch is reported in-flight by growth_touch_index",
      "review_request" in db.growth_touch_index(b5).get(l5, set()))
db.mark_scheduled(id1, "failed")
# Application layer (the gate plays()/scan() actually consult): a FAILED touch is no
# longer in-flight, so the lead can be re-offered/re-queued.
check("W5: a FAILED touch is NOT in-flight in growth_touch_index (plays/scan re-offer)",
      "review_request" not in db.growth_touch_index(b5).get(l5, set()))
id2 = db.add_scheduled_message(b5, l5, None, "review_request", _SA, "next cycle")
check("W5: after the touch FAILS, the unique slot frees -> re-queue succeeds", id2 is not None)
_c = db.get_conn()
_isql = _c.execute("SELECT sql FROM sqlite_master WHERE name='uniq_growth_touch_per_lead'").fetchone()
_c.close()
check("W5: the unique index now excludes 'failed'", bool(_isql) and "failed" in _isql[0])


# ===========================================================================
# W6 -- followup cancels a PENDING quote_followup, leaves HELD untouched
# ===========================================================================
print("\n=== W6: followup vs quote_followup exclusion ===")
b6 = db.create_business({"name": "W6 Co"})
lp = db.create_lead(b6, "Pending Lead", "+15550060001")
lh = db.create_lead(b6, "Held Lead", "+15550060002")
pend = db.add_scheduled_message(b6, lp, None, "quote_followup", _SA, "qf pending")
held = db.add_scheduled_message(b6, lh, None, "quote_followup", _SA, "qf held", status="held")
db.cancel_lead_growth_touches(lp, ("quote_followup",))
check("W6: a PENDING quote_followup is canceled", _status(pend) == "canceled")
db.cancel_lead_growth_touches(lh, ("quote_followup",))
check("W6: a HELD tray quote_followup is left untouched (owner decides)", _status(held) == "held")

# Integration: scan_followups cancels the lead's pending quote_followup when it queues Touch-1.
_calls = []
_o_lb, _o_fr = db.list_businesses, db.followup_candidate_rows
_o_add, _o_out, _o_canc = db.add_scheduled_message, messaging.outbound_mode, db.cancel_lead_growth_touches
_cold = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
db.list_businesses = lambda: [{"id": 99, "name": "X", "followups_enabled": True,
                               "timezone": "America/New_York"}]
db.followup_candidate_rows = lambda bid: [{"id": 77, "name": "Cold", "phone": "+15550060009",
    "has_followup": False, "has_followup_2": False, "last_msg_at": _cold,
    "last_in_text": "hi"}]
db.add_scheduled_message = lambda *a, **k: 555
messaging.outbound_mode = lambda b, p: "live"
db.cancel_lead_growth_touches = lambda lead_id, kinds: _calls.append((lead_id, tuple(kinds)))
try:
    reminders.scan_followups(now=datetime.now(timezone.utc).isoformat())
finally:
    db.list_businesses, db.followup_candidate_rows = _o_lb, _o_fr
    db.add_scheduled_message, messaging.outbound_mode, db.cancel_lead_growth_touches = _o_add, _o_out, _o_canc
check("W6: scan_followups cancels the lead's pending quote_followup on Touch-1",
      (77, ("quote_followup",)) in _calls)


# ===========================================================================
# W7 -- cancel_appointment cancels its reminders atomically
# ===========================================================================
print("\n=== W7: cancel_appointment is atomic ===")
b7 = db.create_business({"name": "W7 Co"})
l7 = db.create_lead(b7, "W7 Lead", "+15550070001")
db.book_appointment(b7, l7, "2026-07-01 14:00", day="2026-07-01", slot_time="14:00")
_c = db.get_conn()
_appt = _c.execute("SELECT id FROM appointments WHERE business_id=? AND lead_id=? "
                   "AND status='booked'", (b7, l7)).fetchone()
_c.close()
appt_id = _appt[0]
rid = db.add_scheduled_message(b7, l7, appt_id, "reminder", _SA, "reminder body")
check("W7 setup: the appointment reminder is pending", _status(rid) == "pending")
res = db.cancel_appointment(b7, appt_id)
check("W7: cancel_appointment returns the canceled row", res is not None)
check("W7: the appointment's reminder is canceled in the SAME call (no orphan)",
      _status(rid) == "canceled")
check("W7: cancel_appointment still rejects a cross-tenant id",
      db.cancel_appointment(999999, appt_id) is None)


# ===========================================================================
# W4 -- tick_once timing is observability-only (inert on a fast tick)
# ===========================================================================
print("\n=== W4: tick_once soft-budget timing ===")
out = reminders.tick_once()
check("W4: tick_once still returns its result dict", isinstance(out, dict) and "sent" in out)


# ---- Results ----
print(f"\n{'='*44}")
print(f"Results: {_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
