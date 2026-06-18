"""Phase 5c CORE: screening graduation + rescue checks.

Covers all spec test cases:
  - graduation fires at >=7d + >=10 would-block verdicts
  - does NOT fire <7d, <10, when screening_hold=1, or non-monitor mode
  - a rescue resets the window so a later pass does NOT promote (ordering gate)
  - rescue increments screening_false_positives + upserts caller as customer
  - burst >=3 adds +35; <3 adds 0 (precision-first: +35 alone < HARD)
  - within_hours filters the spam_flags ledger
  - hard/mid overrides move the verdict band
  - on promotion: screen_mode='enforce', screening_promoted_at set, ONE owner alert
    goes to the owner (never a customer)

Standalone: real temp DB, no network (demo provider, no Twilio).
Exits non-zero on any failure.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ["FIRSTBACK_SCREEN_MODE"] = "monitor"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
# Trigger schema + migration (including the 7 new Phase 5c columns).
db.init_db()

import messaging
messaging.TWILIO_ACCOUNT_SID = ""   # configured() -> False, so sends simulate (no network)

import triage
import alerts
import reminders

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ago(days=0, hours=0, minutes=0, seconds=0):
    """ISO timestamp that many days/hours/minutes/seconds in the past."""
    delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    return (datetime.now(timezone.utc) - delta).isoformat()


def _seed_calls(biz_id, n, status="screened_spam", mode="monitor"):
    """Insert n synthetic missed calls with the given screen_status/screen_mode
    so screening_stats['would_screen'] counts them."""
    conn = db.get_conn()
    for i in range(n):
        conn.execute(
            "INSERT INTO calls (business_id, call_sid, from_number, to_number, "
            "missed, engaged, screen_status, screen_mode, created_at) "
            "VALUES (?,?,?,?,1,0,?,?,?)",
            (biz_id, f"CS{biz_id}_{status}_{i}", f"+1555000{i:04d}", "+15550000000",
             status, mode, db.now_iso()))
    conn.commit()
    conn.close()


def _new_biz(name, screen_mode="monitor", window_start=None, hold=0):
    """Register a new test business with the given screening config. Returns biz dict."""
    from db import _BUSINESS_COLS, DEFAULT_BUSINESS
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO businesses (name, trade, phone) VALUES (?,?,?)",
        (name, "painting", "+15550000001"))
    bid = cur.lastrowid
    conn.execute("UPDATE businesses SET screen_mode=?, screening_hold=? WHERE id=?",
                 (screen_mode, hold, bid))
    if window_start is not None:
        conn.execute("UPDATE businesses SET screening_window_start=? WHERE id=?",
                     (window_start, bid))
    # Ensure alert_on_urgent is NULL (defaults ON -> True) so alerts fire.
    conn.commit()
    conn.close()
    return db.get_business(bid)


def _promoted_at(biz_id):
    conn = db.get_conn()
    row = conn.execute("SELECT screening_promoted_at, screen_mode FROM businesses WHERE id=?",
                       (biz_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def _alert_count(biz_id, kind):
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM alerts WHERE business_id=? AND kind=?",
                     (biz_id, kind)).fetchone()[0]
    conn.close()
    return n


# ═══════════════════════════════════════════════════════════════════════════════
# 1. spam_score burst signal
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- burst signal --")
HARD = config.SCREEN_SCORE_HARD   # default 80

s0, _ = triage.spam_score({"burst_count": 0})
check("burst_count=0: adds 0 to score", s0 == 0)

s2, _ = triage.spam_score({"burst_count": 2})
check("burst_count=2 (below 3): adds 0 to score", s2 == 0)

s3, r3 = triage.spam_score({"burst_count": 3})
check("burst_count=3: adds +35", s3 == 35)
check("burst_count=3: reason mentions 'businesses'", any("businesses" in r for r in r3))

# Precision-first: burst alone must NOT reach HARD (35 < 80).
check("burst_count=3 alone does NOT reach HARD (precision-first)", s3 < HARD)

# Burst + one other weak signal: still corroboration needed for HARD.
s_combo, _ = triage.spam_score({"burst_count": 3, "neighbor_spoof": True})
check("burst + neighbor_spoof = 60, still < HARD", s_combo < HARD)

# Burst + strong corroboration reaches HARD.
s_hard, _ = triage.spam_score({"burst_count": 3,
                                "attestation": "TN-Validation-Failed-C",
                                "neighbor_spoof": True})  # 35+30+25=90
check("burst + attC + neighbor_spoof reaches HARD", s_hard >= HARD)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. global_spam_count within_hours
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- within_hours filter --")
NUM_BURST = "+17775550001"
# Two flags from different businesses: one recent, one old.
biz_a = _new_biz("BizA").get("id")
biz_b = _new_biz("BizB").get("id")

# Direct insert: one flag with an old timestamp (5 days ago), one clearly recent (30 min ago).
# Use 30min-ago for the "recent" flag so a 1h cutoff comfortably includes it.
# Use 5 days for the "old" flag so a 48h window clearly excludes it.
conn = db.get_conn()
old_ts = _ago(days=5)       # 5 days ago -> outside 24h, 48h windows
new_ts = _ago(minutes=30)   # 30 min ago -> inside 1h window, inside 24h window
key = db._digits10(NUM_BURST)
conn.execute("INSERT OR IGNORE INTO spam_flags (business_id, number, created_at) VALUES (?,?,?)",
             (biz_a, key, old_ts))
conn.execute("INSERT OR IGNORE INTO spam_flags (business_id, number, created_at) VALUES (?,?,?)",
             (biz_b, key, new_ts))
conn.commit()
conn.close()

total = db.global_spam_count(NUM_BURST)
check("global_spam_count (no window) = 2", total == 2)

within_1h = db.global_spam_count(NUM_BURST, within_hours=1)
check("within_hours=1: only the 30min-old flag counts (=1)", within_1h == 1)

within_24h = db.global_spam_count(NUM_BURST, within_hours=24)
check("within_hours=24: only the 30min flag (5-day-old is outside 24h window)", within_24h == 1)

within_5min = db.global_spam_count(NUM_BURST, within_hours=0.083)  # 5 min
check("within_hours=5min: zero flags (30min-old flag is outside)", within_5min == 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. screen_caller hard/mid overrides
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- hard/mid overrides --")
# Use a number that scores ~55 (attC=30 + neighbor_spoof=25) -> MID=45 <= 55 < HARD=80
# With no override -> "review" (MID <= 55 < HARD).
# With hard=50 -> "screened_spam" (55 >= 50).
# With mid=60 -> "prospect" (55 < 60).
biz_override = _new_biz("BizOverride").get("id")
BIZ_NUM_OVR = "+15553140001"
db.set_business_twilio(biz_override, BIZ_NUM_OVR, "PNovr")
NUM_55 = "+15553149800"  # neighbor-spoof: same prefix as BIZ_NUM_OVR

v_default = triage.screen_caller(biz_override, NUM_55,
                                  attestation="TN-Validation-Failed-C",
                                  neighbor_spoof=True)
check("default thresholds: 55-score -> review", v_default["status"] == "review")

v_low_hard = triage.screen_caller(biz_override, NUM_55,
                                   attestation="TN-Validation-Failed-C",
                                   neighbor_spoof=True, hard=50)
check("hard=50 override: 55-score -> screened_spam", v_low_hard["status"] == "screened_spam")

v_high_mid = triage.screen_caller(biz_override, NUM_55,
                                   attestation="TN-Validation-Failed-C",
                                   neighbor_spoof=True, mid=60)
check("mid=60 override: 55-score -> prospect", v_high_mid["status"] == "prospect")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. record_screening_rescue
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- record_screening_rescue --")
biz_rescue = _new_biz("BizRescue", screen_mode="monitor",
                       window_start=_ago(days=10)).get("id")
NUM_RESCUED = "+18885550001"
before = db.get_business(biz_rescue)
fp_before = before.get("screening_false_positives") or 0
ws_before = before.get("screening_window_start")

key_back = db.record_screening_rescue(biz_rescue, NUM_RESCUED)
check("record_screening_rescue returns a normalized number key", bool(key_back))

after = db.get_business(biz_rescue)
check("rescue increments screening_false_positives",
      (after.get("screening_false_positives") or 0) == fp_before + 1)

# Window should have reset (later than before).
ws_after = after.get("screening_window_start")
check("rescue resets screening_window_start to now (later than before)",
      ws_after is not None and ws_after > ws_before)

# The caller should now be a 'customer' contact with source='owner-rescue'.
contact = db.get_contact(biz_rescue, NUM_RESCUED)
check("rescue upserts caller as customer", (contact or {}).get("category") == "customer")
check("rescue sets source=owner-rescue", (contact or {}).get("source") == "owner-rescue")

# A second rescue should increment again (idempotent on contact, not on the count).
db.record_screening_rescue(biz_rescue, NUM_RESCUED)
after2 = db.get_business(biz_rescue)
check("second rescue increments false_positives again",
      (after2.get("screening_false_positives") or 0) == fp_before + 2)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. scan_screening_graduation: graduation fires at >=7d + >=10 verdicts
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- graduation: fires correctly --")
biz_grad = _new_biz("BizGrad", screen_mode="monitor",
                     window_start=_ago(days=8)).get("id")
_seed_calls(biz_grad, 10, status="screened_spam", mode="monitor")

n_promoted = reminders.scan_screening_graduation()
check("graduation fires when >=7d + >=10 would-block", n_promoted >= 1)

biz_grad_row = db.get_business(biz_grad)
check("promoted business has screen_mode='enforce'",
      biz_grad_row.get("screen_mode") == "enforce")
check("promoted business has screening_promoted_at set",
      bool(biz_grad_row.get("screening_promoted_at")))

alert_count = _alert_count(biz_grad, "screening_graduated")
check("ONE graduation alert fired for the owner", alert_count == 1)

# A second scan pass must NOT fire another alert (already promoted; long dedupe window).
reminders.scan_screening_graduation()
check("second pass does NOT fire a second alert (dedupe)",
      _alert_count(biz_grad, "screening_graduated") == 1)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. graduation does NOT fire when <7d
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- graduation: does not fire <7d --")
biz_young = _new_biz("BizYoung", screen_mode="monitor",
                      window_start=_ago(days=3)).get("id")
_seed_calls(biz_young, 15)

before_count = _alert_count(biz_young, "screening_graduated")
reminders.scan_screening_graduation()
check("graduation does NOT fire when window is only 3 days old",
      _alert_count(biz_young, "screening_graduated") == before_count)
biz_young_row = db.get_business(biz_young)
check("screen_mode unchanged (still monitor) at <7d",
      (biz_young_row.get("screen_mode") or "monitor") == "monitor")


# ═══════════════════════════════════════════════════════════════════════════════
# 7. graduation does NOT fire when <10 verdicts
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- graduation: does not fire with fewer than 10 verdicts --")
biz_few = _new_biz("BizFew", screen_mode="monitor",
                    window_start=_ago(days=8)).get("id")
_seed_calls(biz_few, 5)  # only 5

reminders.scan_screening_graduation()
check("graduation does NOT fire with only 5 would-block verdicts",
      _alert_count(biz_few, "screening_graduated") == 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. graduation does NOT fire when screening_hold=1
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- graduation: hold defers promotion --")
biz_hold = _new_biz("BizHold", screen_mode="monitor",
                     window_start=_ago(days=10), hold=1).get("id")
_seed_calls(biz_hold, 15)

reminders.scan_screening_graduation()
check("graduation does NOT fire when screening_hold=1",
      _alert_count(biz_hold, "screening_graduated") == 0)
biz_hold_row = db.get_business(biz_hold)
check("screen_mode stays monitor when hold=1",
      (biz_hold_row.get("screen_mode") or "monitor") == "monitor")


# ═══════════════════════════════════════════════════════════════════════════════
# 9. graduation does NOT fire when mode is not monitor
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- graduation: non-monitor modes skipped --")
biz_off = _new_biz("BizOff", screen_mode="off",
                    window_start=_ago(days=10)).get("id")
_seed_calls(biz_off, 15)
reminders.scan_screening_graduation()
check("graduation skips mode=off", _alert_count(biz_off, "screening_graduated") == 0)

biz_enforce = _new_biz("BizEnforce", screen_mode="enforce",
                        window_start=_ago(days=10)).get("id")
_seed_calls(biz_enforce, 15)
reminders.scan_screening_graduation()
check("graduation skips mode=enforce (already there)",
      _alert_count(biz_enforce, "screening_graduated") == 0)


# ═══════════════════════════════════════════════════════════════════════════════
# 10. rescue resets window so a subsequent pass does NOT promote (ordering gate)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- graduation: rescue resets window, blocking immediate promotion --")
# This biz has been in monitor for 10 days (qualifies) but owner just rescued a caller
# (reset window to now). The graduation pass must NOT promote it.
biz_rescued_biz = _new_biz("BizRescuedThenCheck", screen_mode="monitor",
                             window_start=_ago(days=10)).get("id")
_seed_calls(biz_rescued_biz, 12)

# Simulate a rescue: resets window_start to ~now.
db.record_screening_rescue(biz_rescued_biz, "+17775550099")

# Immediately run graduation: must NOT promote (window is now only seconds old).
reminders.scan_screening_graduation()
check("rescue resets clock -> graduation does NOT promote immediately after rescue",
      _alert_count(biz_rescued_biz, "screening_graduated") == 0)
biz_rescued_row = db.get_business(biz_rescued_biz)
check("screen_mode stays monitor after rescue-then-check",
      (biz_rescued_row.get("screen_mode") or "monitor") == "monitor")


# ═══════════════════════════════════════════════════════════════════════════════
# 11. lazy-init NULL window_start: skip the first pass
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- graduation: lazy-init NULL window_start skips first pass --")
biz_null_win = _new_biz("BizNullWin", screen_mode="monitor").get("id")
# Ensure window_start is NULL (no explicit set).
conn = db.get_conn()
conn.execute("UPDATE businesses SET screening_window_start=NULL WHERE id=?",
             (biz_null_win,))
conn.commit()
conn.close()
_seed_calls(biz_null_win, 15)

reminders.scan_screening_graduation()
check("NULL window_start: skip + lazy-init on first pass (no promotion)",
      _alert_count(biz_null_win, "screening_graduated") == 0)
# Window should now be set.
biz_null_row = db.get_business(biz_null_win)
check("window_start was lazy-inited (no longer NULL)",
      bool(biz_null_row.get("screening_window_start")))


# ═══════════════════════════════════════════════════════════════════════════════
# 12. graduation alert goes to the OWNER, not a customer
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- graduation alert: owner-only --")
# Inspect alert content: must never mention the rescued number or any customer.
biz_msg = _new_biz("BizMsg", screen_mode="monitor",
                    window_start=_ago(days=8)).get("id")
_seed_calls(biz_msg, 11)
reminders.scan_screening_graduation()

conn = db.get_conn()
alert_rows = conn.execute(
    "SELECT body FROM alerts WHERE business_id=? AND kind='screening_graduated'",
    (biz_msg,)).fetchall()
conn.close()
check("at least one graduation alert body exists", len(alert_rows) > 0)
for row in alert_rows:
    body = row[0] or ""
    # Must mention blocking/spam (honest about what it did in monitor mode).
    check("graduation alert body mentions 'blocked'", "blocked" in body.lower())
    # Must NOT imply a customer was texted or contacted.
    check("graduation alert body does NOT claim customer was contacted",
          "texted" not in body.lower() and "contacted" not in body.lower())

# format_message: honest plural/singular.
msg_1 = alerts.format_message("screening_graduated", {"n": 1})
check("format_message singular: '1 robocaller'", "1 robocaller" in msg_1)
msg_n = alerts.format_message("screening_graduated", {"n": 7})
check("format_message plural: '7 robocallers'", "7 robocallers" in msg_n)
check("format_message does not say 'contacted'", "contacted" not in msg_n.lower())
check("format_message does not say 'texted'", "texted" not in msg_n.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# 13. promote_screening DB function
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- promote_screening --")
biz_promote = _new_biz("BizPromote", screen_mode="monitor").get("id")
db.promote_screening(biz_promote)
row = db.get_business(biz_promote)
check("promote_screening sets screen_mode='enforce'", row.get("screen_mode") == "enforce")
check("promote_screening sets screening_promoted_at", bool(row.get("screening_promoted_at")))


# ═══════════════════════════════════════════════════════════════════════════════
# 14. alerts.py: screening_graduated registrations
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- alerts.py registration --")
check("screening_graduated in ALERT_KINDS", "screening_graduated" in alerts.ALERT_KINDS)
check("_TOGGLE_COL maps screening_graduated to alert_on_urgent",
      alerts._TOGGLE_COL.get("screening_graduated") == "alert_on_urgent")
check("_subject returns a non-empty string for screening_graduated",
      bool(alerts._subject("screening_graduated")))
check("_dedupe_key returns 'screening_graduated' (stable key)",
      alerts._dedupe_key("screening_graduated", {}) == "screening_graduated")


# ═══════════════════════════════════════════════════════════════════════════════
# 15. config constants
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- config constants --")
check("SCREEN_GRADUATION_DAYS == 7", config.SCREEN_GRADUATION_DAYS == 7)
check("SCREEN_GRADUATION_MIN_VERDICTS == 10", config.SCREEN_GRADUATION_MIN_VERDICTS == 10)
check("SCREEN_SENSITIVITY_PRESETS conservative = (90,55)",
      config.SCREEN_SENSITIVITY_PRESETS.get("conservative") == (90, 55))
check("SCREEN_SENSITIVITY_PRESETS balanced = (80,45)",
      config.SCREEN_SENSITIVITY_PRESETS.get("balanced") == (80, 45))
check("SCREEN_SENSITIVITY_PRESETS aggressive = (65,35)",
      config.SCREEN_SENSITIVITY_PRESETS.get("aggressive") == (65, 35))


# ═══════════════════════════════════════════════════════════════════════════════
# 16. Phase 5c columns exist in the businesses table
# ═══════════════════════════════════════════════════════════════════════════════
print("\n-- DB columns --")
conn = db.get_conn()
cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
conn.close()
for col in ("screening_window_start", "screening_false_positives",
            "screen_hard", "screen_mid", "reputation_enabled",
            "screening_promoted_at", "screening_hold"):
    check(f"businesses has column '{col}'", col in cols)


# ─── Final report ─────────────────────────────────────────────────────────────
print(f"\nResult: {_pass} passed, {_fail} failed")
if _fail:
    sys.exit(1)
