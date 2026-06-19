"""Phase 6b W2 -- unified 8am daily digest.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_daily_digest.py

Proves the consolidation contract:
  A. format_message('daily_digest') is honest + <=320: leads (no "0 leads"), held-plays
     with GO/SKIP, top stall, estimated-money label.
  B. scan_daily_digest fires ONCE at 8am local and dedupes (second tick same day -> 0);
     no fire at 7am/9am.
  C. The digest goes to the OWNER cell only -- ZERO customer sends.
  D. The digest does NOT auto-release held plays (TCPA: only the owner's GO/tap releases).
  E. After the digest, an owner GO releases the held plays (folding growth_tray into the
     digest does not break GO).
  F. Empty state (no leads, no held plays, no stall) -> no digest (don't wake the owner).
  G. With both held plays AND a stall, the ONE body mentions both.

Exits 0 on all pass, 1 if any fail.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

os.environ["FIRSTBACK_PROVIDER"] = "demo"   # deterministic, no network

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

import messaging
messaging.TWILIO_ACCOUNT_SID = ""           # configured() False -> simulates

import alerts
import reminders
import app as appmod                          # for the GO reply handler

_APP_TZ = config.app_tz()
_pass = _fail = 0
_SENT = []   # (to, body) capture


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def _captured_send(business, to, body, **kwargs):
    _SENT.append((to, body))
    return {"status": "simulated"}


messaging.send_sms = _captured_send


def _iso_at_local_hour(h):
    """ISO UTC string whose _APP_TZ-local representation is at hour h today."""
    local = datetime.now(_APP_TZ).replace(hour=h, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).isoformat()


def _make_biz(owner_sms, avg_job_value=None):
    bid = db.create_business({"name": "Digest Test Co"})
    conn = db.get_conn()
    conn.execute("UPDATE businesses SET alert_sms=?, alert_on_daily_digest=1, "
                 "growth_mode='tray' WHERE id=?", (owner_sms, bid))
    if avg_job_value is not None:
        conn.execute("UPDATE businesses SET avg_job_value=? WHERE id=?", (avg_job_value, bid))
    conn.commit(); conn.close()
    return db.get_business(bid)


def _insert_held(bid, lead_id, kind="review_request", body="Held growth msg"):
    send_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    conn = db.get_conn()
    conn.execute("INSERT INTO scheduled_messages "
                 "(business_id, lead_id, kind, send_at, body, status) VALUES (?,?,?,?,?,?)",
                 (bid, lead_id, kind, send_at, body, "held"))
    conn.commit(); conn.close()


def _make_stalled_lead(bid, name, phone, idle_h=50):
    """A warm lead that replied idle_h hours ago (warm_leads_idle should surface it)."""
    lid = db.create_lead(bid, name, phone)
    old = (datetime.now(timezone.utc) - timedelta(hours=idle_h)).isoformat()
    conn = db.get_conn()
    conn.execute("UPDATE leads SET status='replied', created_at=? WHERE id=?", (old, lid))
    conn.execute("INSERT INTO messages (lead_id, direction, body, created_at) "
                 "VALUES (?,?,?,?)", (lid, "in", "any update?", old))
    conn.commit(); conn.close()
    return lid


# ===========================================================================
# A. format_message honesty + cap
# ===========================================================================
print("\n=== A: format_message(daily_digest) ===")
body = alerts.format_message("daily_digest", {
    "n_leads": 3, "money": "$6,000", "is_estimated": True,
    "plays_count": 2, "plays_summary": "1) Maria (review), 2) Carlos (win-back)",
    "top_stall_name": "Maria", "top_stall_hours": 26, "local_day": "2026-06-19"})
check("A: body <= 320 chars", len(body) <= 320)
check("A: mentions the leads count", "3 leads need you" in body)
check("A: labels estimated money", "(est.)" in body)
check("A: has the GO/SKIP instruction", "Reply GO to send all" in body)
check("A: surfaces the top stall", "One stall: Maria 26h" in body)
check("A: never 'tap to send' for leads", "tap to send" not in body.lower())
# No-leads variant must not say "0 leads".
body0 = alerts.format_message("daily_digest", {
    "n_leads": 0, "plays_count": 1, "plays_summary": "1) Sam (review)",
    "top_stall_name": "", "local_day": "2026-06-19"})
check("A: zero leads omits the leads clause (no '0 leads')", "0 lead" not in body0)
check("A: zero-leads body still has GO", "Reply GO" in body0)
# Long input still caps at 320.
bodyL = alerts.format_message("daily_digest", {
    "n_leads": 9, "money": "$99,000", "is_estimated": False, "plays_count": 10,
    "plays_summary": ", ".join(f"{i}) LongCustomerName{i} (reactivation)" for i in range(1, 11)),
    "top_stall_name": "Bartholomew", "top_stall_hours": 72, "local_day": "2026-06-19"})
check("A: long inputs still cap at 320", len(bodyL) <= 320)


# ===========================================================================
# B. fires once at 8am, dedupes; not at 7am/9am
# ===========================================================================
print("\n=== B: 8am fire + dedupe ===")
_SENT.clear()
bizB = _make_biz("+15550020001")
lidB = db.create_lead(bizB["id"], "Held Lead", "+15559992001")
_insert_held(bizB["id"], lidB)

check("B: no fire at 7am", reminders.scan_daily_digest(_iso_at_local_hour(7)) == 0)
check("B: no fire at 9am", reminders.scan_daily_digest(_iso_at_local_hour(9)) == 0)
fired1 = reminders.scan_daily_digest(_iso_at_local_hour(8))
own_B = [r for r in _SENT if r[0] == bizB["alert_sms"]]
check("B: fires at 8am with held plays", fired1 >= 1 and len(own_B) == 1)
fired2 = reminders.scan_daily_digest(_iso_at_local_hour(8))
check("B: dedupes -- second 8am tick same day fires 0", fired2 == 0)
check("B: still only one SMS after the second tick",
      len([r for r in _SENT if r[0] == bizB["alert_sms"]]) == 1)


# ===========================================================================
# C. owner-cell only -- zero customer sends
# ===========================================================================
print("\n=== C: owner-cell only ===")
check("C: the digest went to the owner cell", own_B and own_B[0][0] == bizB["alert_sms"])
check("C: the lead phone received ZERO texts",
      not any(r[0] == "+15559992001" for r in _SENT))


# ===========================================================================
# D + E. digest does not auto-release; GO releases
# ===========================================================================
print("\n=== D/E: held plays stay held until GO ===")
check("D: held play is STILL held after the digest (no auto-release)",
      len(db.list_held_messages(bizB["id"])) == 1)
# Owner replies GO -> release.
appmod._handle_tray_reply(bizB, {"cmd": "go"})
check("E: owner GO after the digest releases the held plays",
      len(db.list_held_messages(bizB["id"])) == 0)


# ===========================================================================
# F. empty state -> no digest
# ===========================================================================
print("\n=== F: empty state ===")
_SENT.clear()
bizF = _make_biz("+15550020009")
firedF = reminders.scan_daily_digest(_iso_at_local_hour(8))
check("F: a business with no leads/plays/stall fires no digest",
      not any(r[0] == bizF["alert_sms"] for r in _SENT))


# ===========================================================================
# G. combines plays + stall in ONE body
# ===========================================================================
print("\n=== G: one SMS combines plays + stall ===")
_SENT.clear()
bizG = _make_biz("+15550020020")           # avg_job_value None -> estimated
lidG = db.create_lead(bizG["id"], "Plays Lead", "+15559992020")
_insert_held(bizG["id"], lidG)
_make_stalled_lead(bizG["id"], "Maria Stall", "+15559992021", idle_h=50)
reminders.scan_daily_digest(_iso_at_local_hour(8))
own_G = [b for (to, b) in _SENT if to == bizG["alert_sms"]]
check("G: exactly one digest SMS for the combined business", len(own_G) == 1)
if own_G:
    dg = own_G[0]
    check("G: the one body mentions the held plays (ready / GO)", "Reply GO" in dg)
    check("G: the one body mentions the stall", "One stall:" in dg)
    check("G: still <= 320 chars", len(dg) <= 320)
    check("G: no customer phone received the digest",
          not any(to.startswith("+15559992") for (to, b) in _SENT))


# ---- Results ----------------------------------------------------------------
print(f"\n{'='*44}")
print(f"Results: {_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
