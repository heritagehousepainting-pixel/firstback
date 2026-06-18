"""SF-8 persistence tests. Run: python test_sf8_persist.py

Covers (real DB, no Twilio):
  - send_sms blocked branch -> a blocked_sends row is created
  - queue_blocked_send: returns int id or None on error
  - get_blocked_sends: unflushed by default; flushed=True returns completed rows
  - mark_flushed: sets flushed=1, flushed_at
  - mark_flush_skipped: sets flushed=1, skip_reason
  - Migration idempotency: calling init_db() twice leaves columns + table + index intact

Standalone; no network; exit non-zero on failure.
"""
import os
import sys
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest_sf8p")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_sf8p")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+15550000077")

import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name
config.TWILIO_TRUST_PRODUCT_SID = ""

import db
db.DB_PATH = _TMP.name
db.DB_BACKUP_PATH = ""

# First init_db()
db.init_db()

import messaging
import requests as _rq

# ---- Network tripwire: real Twilio POSTs must fail loudly ----
class _NetworkLeak(BaseException):
    pass

def _no_net(*a, **kw):
    raise _NetworkLeak(f"unstubbed network call: {a[0] if a else '?'}")

_rq.post = _no_net
_rq.get = _no_net

# Patch messaging module so configured() is True but a2p gate returns blocked
messaging.TWILIO_ACCOUNT_SID = "ACtest_sf8p"
messaging.TWILIO_AUTH_TOKEN = "tok_sf8p"
messaging.TWILIO_FROM_NUMBER = "+15550000077"
messaging.TWILIO_TRUST_PRODUCT_SID = ""

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ============================================================
# 1. init_db() idempotency: run it a second time
# ============================================================
db.init_db()  # second call

# Verify Phase 3 columns exist after double-init
conn = db.get_conn()
biz_cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
conn.close()
check("init_db twice: business_type column exists",
      "business_type" in biz_cols)
check("init_db twice: micro_site_slug column exists",
      "micro_site_slug" in biz_cols)
check("init_db twice: a2p_contact_email column exists",
      "a2p_contact_email" in biz_cols)

# Verify blocked_sends table exists
conn = db.get_conn()
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
indexes = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
conn.close()
check("init_db twice: blocked_sends table exists",
      "blocked_sends" in tables)
check("init_db twice: idx_blocked_sends_biz index exists",
      "idx_blocked_sends_biz" in indexes)


# ============================================================
# 2. set_business_type and set_micro_site
# ============================================================
db.set_business_type(1, "sole_prop")
biz = db.get_business(1)
check("set_business_type: persists sole_prop",
      biz.get("business_type") == "sole_prop")

db.set_business_type(1, "llc")
biz = db.get_business(1)
check("set_business_type: persists llc",
      biz.get("business_type") == "llc")

db.set_micro_site(1, "heritage-painting-1", "heritage-painting-1@clients.firstback.com")
biz = db.get_business(1)
check("set_micro_site: micro_site_slug persisted",
      biz.get("micro_site_slug") == "heritage-painting-1")
check("set_micro_site: a2p_contact_email persisted",
      biz.get("a2p_contact_email") == "heritage-painting-1@clients.firstback.com")


# ============================================================
# 3. queue_blocked_send / get_blocked_sends round-trip
# ============================================================
lead_id = db.create_lead(1, "Blocked Test Lead", "+15559990001")

row_id = db.queue_blocked_send(1, lead_id, "+15559990001", "Thanks for calling, let me follow up!")
check("queue_blocked_send: returns int id",
      isinstance(row_id, int) and row_id > 0)

rows = db.get_blocked_sends(1, flushed=False)
check("get_blocked_sends: returns at least one unflushed row",
      len(rows) >= 1)

found = next((r for r in rows if r["id"] == row_id), None)
check("get_blocked_sends: correct body",
      found is not None and found["body"] == "Thanks for calling, let me follow up!")
check("get_blocked_sends: correct to_number",
      found is not None and found["to_number"] == "+15559990001")
check("get_blocked_sends: correct lead_id",
      found is not None and found["lead_id"] == lead_id)
check("get_blocked_sends: flushed=0 initially",
      found is not None and found["flushed"] == 0)
check("get_blocked_sends: skip_reason is None initially",
      found is not None and found["skip_reason"] is None)


# ============================================================
# 4. mark_flushed
# ============================================================
row_id_flush = db.queue_blocked_send(1, lead_id, "+15559990001", "Second blocked send")
check("queue_blocked_send: second row id returned",
      isinstance(row_id_flush, int) and row_id_flush != row_id)

db.mark_flushed(row_id_flush)

# Check via direct query
conn = db.get_conn()
r = conn.execute("SELECT * FROM blocked_sends WHERE id=?", (row_id_flush,)).fetchone()
conn.close()
check("mark_flushed: flushed=1",
      r is not None and r["flushed"] == 1)
check("mark_flushed: flushed_at set",
      r is not None and r["flushed_at"] is not None)
check("mark_flushed: skip_reason still None",
      r is not None and r["skip_reason"] is None)

# Should NOT appear in get_blocked_sends(flushed=False)
unflushed = db.get_blocked_sends(1, flushed=False)
check("mark_flushed: row absent from unflushed queue",
      not any(row["id"] == row_id_flush for row in unflushed))

# Should appear in get_blocked_sends(flushed=True)
flushed_rows = db.get_blocked_sends(1, flushed=True)
check("mark_flushed: row appears in flushed=True query",
      any(row["id"] == row_id_flush for row in flushed_rows))


# ============================================================
# 5. mark_flush_skipped
# ============================================================
row_id_skip = db.queue_blocked_send(1, lead_id, "+15559990001", "Stale blocked send")
db.mark_flush_skipped(row_id_skip, "stale")

conn = db.get_conn()
r = conn.execute("SELECT * FROM blocked_sends WHERE id=?", (row_id_skip,)).fetchone()
conn.close()
check("mark_flush_skipped: flushed=1",
      r is not None and r["flushed"] == 1)
check("mark_flush_skipped: skip_reason='stale'",
      r is not None and r["skip_reason"] == "stale")
check("mark_flush_skipped: flushed_at set",
      r is not None and r["flushed_at"] is not None)


# ============================================================
# 6. send_sms blocked branch -> queue_blocked_send persists a row
# ============================================================
# Business must be in a state where: configured()=True, a2p_ready=False
# We mock compliance.a2p_ready to return False
import compliance as _compliance
_orig_a2p_ready = _compliance.a2p_ready
_compliance.a2p_ready = lambda b: False

# Count blocked_sends before
rows_before = db.get_blocked_sends(1, flushed=False)
n_before = len(rows_before)

biz_dict = db.get_business(1)
biz_dict["id"] = 1
result = messaging.send_sms(biz_dict, "+15559990002", "You just missed me -- following up!",
                             lead_id=lead_id)
check("send_sms blocked branch: returns blocked status",
      result.get("status") == "blocked")
check("send_sms blocked branch: reason is a2p_not_approved",
      result.get("reason") == "a2p_not_approved")

rows_after = db.get_blocked_sends(1, flushed=False)
n_after = len(rows_after)
check("send_sms blocked branch: a new blocked_sends row was created",
      n_after == n_before + 1)

newest = sorted(rows_after, key=lambda r: r["id"])[-1]
check("send_sms blocked branch: row has correct body",
      newest["body"] == "You just missed me -- following up!")
check("send_sms blocked branch: row has correct to_number",
      newest["to_number"] == "+15559990002")
check("send_sms blocked branch: row has correct lead_id",
      newest["lead_id"] == lead_id)

_compliance.a2p_ready = _orig_a2p_ready


# ============================================================
# 7. send_sms blocked with no lead_id: no blocked_sends row
# ============================================================
_compliance.a2p_ready = lambda b: False
rows_before_no_lead = db.get_blocked_sends(1, flushed=False)
n_before_no_lead = len(rows_before_no_lead)

result_no_lead = messaging.send_sms(biz_dict, "+15559990003", "No lead id send", lead_id=None)
rows_after_no_lead = db.get_blocked_sends(1, flushed=False)
check("send_sms blocked, no lead_id: no blocked_sends row added",
      len(rows_after_no_lead) == n_before_no_lead)

_compliance.a2p_ready = _orig_a2p_ready


# ============================================================
# 8. get_blocked_sends ordering and limit
# ============================================================
lead2 = db.create_lead(1, "Order Test", "+15551112222")
for i in range(5):
    db.queue_blocked_send(1, lead2, "+15551112222", f"Message {i}")

ordered = db.get_blocked_sends(1, flushed=False)
# Should be ordered oldest-first (ascending blocked_at)
ids = [r["id"] for r in ordered if r["lead_id"] == lead2]
check("get_blocked_sends: results include new rows",
      len(ids) == 5)
check("get_blocked_sends: ordered ascending by blocked_at (oldest first)",
      ids == sorted(ids))


# ============================================================
# 9. queue_blocked_send with explicit blocked_at
# ============================================================
explicit_at = "2026-01-01T10:00:00+00:00"
lead3 = db.create_lead(1, "Explicit AT Lead", "+15553334444")
row_explicit = db.queue_blocked_send(1, lead3, "+15553334444", "Past send", blocked_at=explicit_at)
conn = db.get_conn()
r = conn.execute("SELECT * FROM blocked_sends WHERE id=?", (row_explicit,)).fetchone()
conn.close()
check("queue_blocked_send: explicit blocked_at stored correctly",
      r is not None and r["blocked_at"] == explicit_at)


# ============================================================
# Cleanup
# ============================================================
import os as _os
_os.unlink(_TMP.name)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
