"""Batch E2a -- Progressive ROI milestones (plan 07-3). Run: python3 test_roi_progressive.py

check_roi_milestone now returns the HIGHEST newly-crossed level among [2,5,10,25] and only
ever moves UP (UNIQUE-guarded roi_milestones table). Back-compat: roi_milestone_sent_at
counts as level 2 fired. Copy carries the loss-framing tail + the estimate source label.
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

import compliance
import roi

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# Control the multiple deterministically; assume A2P ready.
_MULT = [50.0]
compliance.a2p_ready = lambda b: True
db.analytics = lambda bid, days=None: {
    "totals": {"booked": 3}, "roi_multiple": _MULT[0], "revenue": 9000, "avg_source": "owner"}


def _biz(sent_at=None):
    bid = db.create_business({"name": "ROI Biz"})
    if sent_at:
        db.set_roi_milestone_sent(bid, sent_at)
    return bid


# --- db helper round-trip + idempotency ---
b = _biz()
db.mark_roi_milestone(b, 5, 9000)
db.mark_roi_milestone(b, 5, 9000)  # duplicate -> ignored
levels = [r["level"] for r in db.get_roi_milestones(b)]
check("mark + get round-trip", levels == [5])
check("mark is idempotent (UNIQUE business_id, level)", levels.count(5) == 1)

# --- highest crossed fires first; never walks back down ---
b1 = _biz()
_MULT[0] = 50.0
m = roi.check_roi_milestone(b1)
check("at 50x with nothing fired -> returns the top level (25), not 2", m and m["level"] == 25)
db.mark_roi_milestone(b1, m["level"], m["revenue"])
check("after firing 25, nothing higher -> None (never fires 10/5/2 after)",
      roi.check_roi_milestone(b1) is None)

# --- genuine progression over time ---
b2 = _biz()
_MULT[0] = 6.0
m = roi.check_roi_milestone(b2)
check("at 6x -> fires level 5 (highest crossed)", m and m["level"] == 5)
db.mark_roi_milestone(b2, m["level"], m["revenue"])
check("at 6x again after firing 5 -> None", roi.check_roi_milestone(b2) is None)
_MULT[0] = 12.0
m = roi.check_roi_milestone(b2)
check("multiple grows to 12x -> next level 10 fires", m and m["level"] == 10)
db.mark_roi_milestone(b2, m["level"], m["revenue"])
check("at 12x after firing 10 -> None (25 not yet crossed)",
      roi.check_roi_milestone(b2) is None)

# --- back-compat: roi_milestone_sent_at counts as level 2 fired ---
b3 = _biz(sent_at="2026-06-17T10:00:00+00:00")
_MULT[0] = 3.0
check("legacy sent + 3x (no higher level) -> None (no re-fire of 2)",
      roi.check_roi_milestone(b3) is None)
b4 = _biz(sent_at="2026-06-17T10:00:00+00:00")
_MULT[0] = 18.0
m = roi.check_roi_milestone(b4)
check("legacy sent + 18x -> progresses to level 10", m and m["level"] == 10)

# --- gates still hold ---
b5 = _biz()
_MULT[0] = 1.5
check("below 2x -> None", roi.check_roi_milestone(b5) is None)
_MULT[0] = 50.0
compliance.a2p_ready = lambda b: False
check("a2p pending -> None", roi.check_roi_milestone(b5) is None)
compliance.a2p_ready = lambda b: True

# --- honest copy at every level ---
for lvl in (2, 5, 10, 25):
    body = roi._milestone_body(lvl, 12345, "owner", float(lvl))
    check(f"level {lvl} copy has the loss-framing tail",
          "without firstback" in body.lower())
    check(f"level {lvl} copy labels the estimate (no cash/actual/collected)",
          "estimate" in body.lower()
          and not any(w in body.lower() for w in ("cash", "actual", "collected")))
    check(f"level {lvl} owner copy names 'your average job value'",
          "your average job value" in body.lower())

# --- per-level dedupe key (audit P1 fix): different levels must not collapse ---
import alerts
check("roi_milestone dedupe key is per-level (different levels differ)",
      alerts._dedupe_key("roi_milestone", {"level": 5})
      != alerts._dedupe_key("roi_milestone", {"level": 10}))
check("roi_milestone dedupe key stable for the same level",
      alerts._dedupe_key("roi_milestone", {"level": 5})
      == alerts._dedupe_key("roi_milestone", {"level": 5}))

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
