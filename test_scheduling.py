"""Scheduling preferences + buffer checks (Phase 1). Run: python3 test_scheduling.py

Proves the owner can shape their own availability and the AI never books two estimates
too close to make both:
  * default behavior is unchanged (config ESTIMATE_TIMES, Mon-Sat, no buffer),
  * per-business estimate windows + working days are honored by upcoming_slots,
  * a booking blocks any window within buffer_minutes that day (the 2pm/3pm problem),
  * the Settings form persists the preferences.
Throwaway temp DB + the deterministic demo brain; no network.
"""
import os
import tempfile
from datetime import timedelta

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
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


def first_working_day(working):
    """The soonest upcoming date whose weekday is in `working`."""
    cur = db._today()
    for _ in range(1, config.BOOKING_HORIZON_DAYS + 1):
        cur = cur + timedelta(days=1)
        if cur.weekday() in working:
            return cur
    return None


# --- defaults unchanged for a fresh tenant ---
prefs = db.scheduling_prefs(1)
check("default times fall back to config ESTIMATE_TIMES", prefs["times"] == config.ESTIMATE_TIMES)
check("default working days are Mon-Sat", prefs["working_days"] == set(config.DEFAULT_WORKING_DAYS))
check("default buffer is 0 (original behavior)", prefs["buffer_minutes"] == 0)
slots = db.upcoming_slots(1)
check("default slots are offered", len(slots) > 0)
check("default slots never fall on Sunday",
      all(db.date.fromisoformat(s["day"]).weekday() != 6 for s in slots))

# --- per-business estimate windows are honored ---
db.set_scheduling_prefs(1, times=["1:00 PM", "2:00 PM"])
slots = db.upcoming_slots(1)
offered_keys = {s["time_key"] for s in slots}
check("only the owner's windows are offered", offered_keys <= {"13:00", "14:00"})
check("both custom windows show up", {"13:00", "14:00"} <= offered_keys)

# --- buffer blocks an adjacent window (the 2pm/3pm problem) ---
db.set_scheduling_prefs(1, times=["1:00 PM", "2:00 PM"], buffer_minutes=90)
day = first_working_day(db.scheduling_prefs(1)["working_days"])
lead = db.create_lead(1, "Dana", "+15550000001")
booked = db.book_appointment(1, lead, f"{day.isoformat()} 1:00 PM", day=day.isoformat(),
                             slot_time="13:00")
check("the 1pm estimate books", booked is True)
slots = db.upcoming_slots(1)
ids = {s["id"] for s in slots}
check("the booked 1pm is no longer offered", f"{day.isoformat()}@13:00" not in ids)
check("the 2pm that day is blocked by the 90-min buffer (60<90)",
      f"{day.isoformat()}@14:00" not in ids)
# ...but the same windows on a DIFFERENT open day are still free.
later = first_working_day(db.scheduling_prefs(1)["working_days"])
nxt = later + timedelta(days=1)
while nxt.weekday() not in db.scheduling_prefs(1)["working_days"]:
    nxt = nxt + timedelta(days=1)
check("a 2pm on another open day is still offered", f"{nxt.isoformat()}@14:00" in ids)

# --- working days exclude closed days ---
db.set_scheduling_prefs(1, working_days=[0, 1, 2, 3, 4])   # Mon-Fri only
slots = db.upcoming_slots(1)
check("no weekend slots when working days are Mon-Fri",
      all(db.date.fromisoformat(s["day"]).weekday() < 5 for s in slots))

# --- a blank/zeroing entry safely falls back, never zero availability ---
db.set_scheduling_prefs(1, times=[], working_days=[])
prefs = db.scheduling_prefs(1)
check("blank times fall back to default", prefs["times"] == config.ESTIMATE_TIMES)
check("blank working days fall back to default", prefs["working_days"] == set(config.DEFAULT_WORKING_DAYS))

# --- the Settings form persists preferences ---
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})
with client.session_transaction() as _s:
    _s["csrf_token"] = "test_csrf"
client.environ_base["HTTP_X_CSRF_TOKEN"] = "test_csrf"
r = client.get("/settings")
check("settings page renders the scheduling card",
      r.status_code == 200 and b"Estimate windows" in r.data and b"Working days" in r.data)
client.post("/settings", data={
    "name": "Heritage", "trade": "Painting", "service_area": "", "hours": "",
    "owner_name": "", "phone": "", "ai_instructions": "",
    "estimate_times": "10:00 AM, 3:00 PM", "buffer_minutes": "120",
    "working_days": ["1", "2", "3"]})
prefs = db.scheduling_prefs(1)
check("settings POST saves estimate windows", {db.time_key(t) for t in prefs["times"]} == {"10:00", "15:00"})
check("settings POST saves the buffer", prefs["buffer_minutes"] == 120)
check("settings POST saves working days", prefs["working_days"] == {1, 2, 3})

print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
import sys
sys.exit(1 if _fail else 0)
