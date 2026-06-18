"""Phase-4 C: Milestone hook tests.  Run: python test_f12_milestone_hook.py

Covers:
  1. A booking that makes roi.check_roi_milestone return a dict fires
     alerts.notify_async("roi_milestone") exactly once.
  2. A booking that makes roi.check_roi_milestone return a dict also calls
     db.set_roi_milestone_sent with the business id.
  3. When roi.check_roi_milestone returns None, no "roi_milestone" alert fires.
  4. When roi.check_roi_milestone returns None, db.set_roi_milestone_sent is NOT called.
  5. A second booking for the same business (roi returns None now) does NOT re-fire.
  6. A booking failure (db.book_appointment returns False) does NOT trigger the hook.
  7. roi.check_roi_milestone raising an exception does NOT crash handle_inbound.

Stubs:
  - roi.check_roi_milestone(business_id) -> dict | None
  - db.set_roi_milestone_sent(business_id, ts)
  - db.get_last_inbound_message(lead_id) -> str (for dispatcher path)
  - db.set_dispatcher_call_at(lead_id, ts) (for dispatcher path)
  - ai.detect_urgency -> False (not testing urgency here)
  - ai.generate_reply -> books a specific slot
"""

import os
import sys
import tempfile
import types

os.environ.setdefault("FIRSTBACK_PROVIDER", "demo")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://ringback-gixe.onrender.com")
os.environ.setdefault("FIRSTBACK_TASKS_SECRET", "tasks_secret_test")
os.environ.setdefault("FIRSTBACK_VOICE_PUBLIC_URL", "")  # no dispatcher calls

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

# ---- Stub Agent A's db functions ----
_milestone_sent_calls = []  # list of (business_id, ts)
_dispatcher_call_store = {}


def _stub_set_roi_milestone_sent(business_id, ts):
    _milestone_sent_calls.append((business_id, ts))


def _stub_set_dispatcher_call_at(lead_id, ts):
    _dispatcher_call_store[lead_id] = ts


def _stub_get_last_inbound_message(lead_id):
    return ""


db.set_roi_milestone_sent = _stub_set_roi_milestone_sent
db.set_dispatcher_call_at = _stub_set_dispatcher_call_at
db.get_last_inbound_message = _stub_get_last_inbound_message

# ---- Stub roi module (Agent A) ----
_roi_check_calls = []
_roi_return_value = None  # will be set per test


def _stub_check_roi_milestone(business_id):
    _roi_check_calls.append(business_id)
    return _roi_return_value


_roi_mod = types.ModuleType("roi")
_roi_mod.check_roi_milestone = _stub_check_roi_milestone
sys.modules["roi"] = _roi_mod

# ---- Capture alerts.notify_async calls ----
import alerts as _alerts_mod
_notify_calls = []
_orig_notify_async = _alerts_mod.notify_async


def _capture_notify(biz, kind, ctx=None, **kw):
    _notify_calls.append({"kind": kind, "ctx": ctx or {}})


_alerts_mod.notify_async = _capture_notify

# ---- Stub ai to control bookings ----
import ai as _ai_mod
_orig_detect_urgency = _ai_mod.detect_urgency
_orig_gen_reply = _ai_mod.generate_reply
_ai_mod.detect_urgency = lambda body: False

# The slot used for tests — must be in the future and a valid key
SLOT_KEY = "next-monday-9am"

import app as _app

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ---- Set up business and lead ----
db.set_business_twilio(1, "+15553140000", "PN1")
db.update_a2p_profile(1, {"ein": "12-3456789",
                           "business_address": "1 Main St, Philadelphia PA"})
db.set_a2p_status(1, "approved")
biz = dict(db.get_business(1))

# Give the business an avg_job_value so revenue calc is clean
# (db.set_avg_job_value may not exist yet in the worktree — skip if absent)
if hasattr(db, "set_avg_job_value"):
    db.set_avg_job_value(1, 1000)


def _make_lead(name="Milestone Lead", phone="+15557778888"):
    lid = db.create_lead(1, name, phone)
    return db.get_lead(lid)


# ===========================================================================
# 1 + 2. Booking + roi returns milestone dict -> alert fired + set_roi_milestone_sent
# ===========================================================================
_roi_return_value = {
    "multiple": 2.5,
    "revenue": 2500,
    "avg_source": "owner",
    "body": "FirstBack has booked an estimated ~$2500 in jobs — about 2.5x its cost.",
}
_milestone_sent_calls.clear()
_roi_check_calls.clear()
_notify_calls.clear()

lead1 = _make_lead()
# Stub ai to book this lead at a fresh slot
_booking_slot = None


def _gen_reply_book(biz_, history, **kw):
    # Return a valid slot key that db.book_appointment will accept.
    # Use db.upcoming_slots to get a real available slot.
    try:
        slots = db.upcoming_slots(biz_["id"])
        if slots:
            return ("Great, booked!", slots[0]["id"])
    except Exception:
        pass
    return ("Sorry, no slots available.", None)


_ai_mod.generate_reply = _gen_reply_book

reply1, booked1, urgent1 = _app.handle_inbound(biz, lead1, "I need an estimate please.")

milestone_alerts = [c for c in _notify_calls if c["kind"] == "roi_milestone"]
check("milestone fired: roi_milestone alert sent once",
      len(milestone_alerts) == 1)
check("milestone fired: alert body contains milestone text",
      "2500" in str(milestone_alerts[0]["ctx"].get("body", ""))
      if milestone_alerts else False)
check("milestone fired: db.set_roi_milestone_sent called",
      len(_milestone_sent_calls) >= 1)
check("milestone fired: set_roi_milestone_sent called with biz id=1",
      any(c[0] == 1 for c in _milestone_sent_calls))


# ===========================================================================
# 3 + 4. roi returns None -> no alert, no db call
# ===========================================================================
_roi_return_value = None
_milestone_sent_calls.clear()
_roi_check_calls.clear()
_notify_calls.clear()

lead2 = _make_lead("No Milestone Lead", "+15557770001")
reply2, booked2, urgent2 = _app.handle_inbound(biz, lead2, "I need an estimate please.")

# Whether booked2 is truthy depends on available slots; what we care about is:
# if roi returned None, no milestone alert fires.
milestone_alerts2 = [c for c in _notify_calls if c["kind"] == "roi_milestone"]
check("roi returns None: no roi_milestone alert fires", len(milestone_alerts2) == 0)
check("roi returns None: set_roi_milestone_sent NOT called",
      len(_milestone_sent_calls) == 0)


# ===========================================================================
# 5. Second booking after roi=None still does not re-fire
# ===========================================================================
_roi_return_value = None  # stays None (already "sent")
_milestone_sent_calls.clear()
_notify_calls.clear()

lead3 = _make_lead("Re-Book Lead", "+15557770002")
_app.handle_inbound(biz, lead3, "Can we reschedule?")

milestone_alerts3 = [c for c in _notify_calls if c["kind"] == "roi_milestone"]
check("second booking with roi=None: still no roi_milestone alert",
      len(milestone_alerts3) == 0)


# ===========================================================================
# 6. Booking failure (db.book_appointment returns False) -> no milestone hook
# ===========================================================================
_roi_return_value = {"multiple": 3.0, "revenue": 3000, "avg_source": "owner",
                     "body": "Great milestone!"}
_milestone_sent_calls.clear()
_notify_calls.clear()

# Stub db.book_appointment to always fail
_orig_book = db.book_appointment
db.book_appointment = lambda *a, **k: False

lead4 = _make_lead("Failed Booking Lead", "+15557770003")
_app.handle_inbound(biz, lead4, "Please book me.")

milestone_alerts4 = [c for c in _notify_calls if c["kind"] == "roi_milestone"]
check("booking failure: no roi_milestone alert fires", len(milestone_alerts4) == 0)
check("booking failure: set_roi_milestone_sent NOT called",
      len(_milestone_sent_calls) == 0)

db.book_appointment = _orig_book


# ===========================================================================
# 7. roi.check_roi_milestone raising an exception does NOT crash handle_inbound
# ===========================================================================
def _stub_check_raises(business_id):
    raise RuntimeError("Database exploded!")


_roi_mod.check_roi_milestone = _stub_check_raises
_milestone_sent_calls.clear()
_notify_calls.clear()

lead5 = _make_lead("Exception Lead", "+15557770004")
try:
    reply5, booked5, urgent5 = _app.handle_inbound(biz, lead5,
                                                    "Need estimate please book me.")
    check("roi exception: handle_inbound does NOT crash", True)
except Exception as e:
    check(f"roi exception: handle_inbound does NOT crash (raised: {e})", False)

_roi_mod.check_roi_milestone = _stub_check_roi_milestone


# ---- Restore ----
_alerts_mod.notify_async = _orig_notify_async
_ai_mod.detect_urgency = _orig_detect_urgency
_ai_mod.generate_reply = _orig_gen_reply


print(f"==== {_pass} passed, {_fail} failed ====")
sys.exit(1 if _fail else 0)
