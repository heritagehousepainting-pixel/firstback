"""Pre-deploy audit fixes -- regression tests for the launch-blockers.
Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_predeploy_fixes.py

  C1  growth marketing kinds send transactional=False (quiet-hours gated); reminders stay True.
  M1  a status='error' Stripe event is NOT 'seen' (re-processes on retry); 'ok' is.
  M2  plan resolution: the actual billed price WINS over stale checkout metadata.
  D1  a password-reset token is single-use (atomic claim).
  L1  cancel_appointment also cancels the morning_reminder (no orphan).
  A1  /api/appointments/<id>/cancel requires CSRF (forged POST -> 403, no customer SMS).
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+15550000000")
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
db.init_db()
import messaging
messaging.TWILIO_ACCOUNT_SID = ""
import reminders
import billing
import app as appmod   # import EARLY so the seed owner/business is created on a fresh DB
                       # (app only seeds when no business exists yet -- before we add any)

_pass = _fail = 0
def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1; print(f"  ok   {name}")
    else:
        _fail += 1; print(f"FAIL   {name}")

_SA = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


# ===========================================================================
# C1 -- growth marketing kinds are quiet-hours gated (transactional=False)
# ===========================================================================
print("\n=== C1: growth kinds send transactional=False ===")
_sends = []
_orig_send = messaging.send_sms
messaging.send_sms = lambda biz, to, body, **kw: (_sends.append((kw.get("transactional", True), body)),
                                                  {"status": "simulated"})[1]
bc = db.create_business({"name": "C1 Co"})
lc = db.create_lead(bc, "C1 Lead", "+15551110001")
db.add_scheduled_message(bc, lc, None, "review_request", _SA, "review marketing")
db.add_scheduled_message(bc, lc, None, "reminder", _SA, "your estimate reminder")
reminders.run_due_once(datetime.now(timezone.utc).isoformat())
messaging.send_sms = _orig_send
_review = [t for (t, b) in _sends if "review marketing" in b]
_remind = [t for (t, b) in _sends if "estimate reminder" in b]
check("C1: growth review_request sent transactional=False (quiet-hours gated)",
      _review == [False])
check("C1: a booking reminder stays transactional=True (quiet-hours exempt)",
      _remind == [True])


# ===========================================================================
# M1 -- a status='error' Stripe event does NOT count as seen
# ===========================================================================
print("\n=== M1: error events re-process; ok events dedupe ===")
db.mark_stripe_event("evt_err_1", "invoice.paid", status="error", detail="SQLITE_BUSY")
check("M1: an 'error' event is NOT seen (Stripe retry re-processes)",
      db.stripe_event_seen("evt_err_1") is False)
db.mark_stripe_event("evt_ok_1", "invoice.paid", status="ok")
check("M1: an 'ok' event IS seen (deduped)", db.stripe_event_seen("evt_ok_1") is True)
# A retry that succeeds upgrades the error row to ok -> then deduped.
db.mark_stripe_event("evt_err_1", "invoice.paid", status="ok")
check("M1: after a successful retry the event becomes seen",
      db.stripe_event_seen("evt_err_1") is True)


# ===========================================================================
# M2 -- the actual billed price wins over stale checkout metadata
# ===========================================================================
print("\n=== M2: billed price wins over stale metadata ===")
_orig_prices = billing.PRICE_IDS
billing.PRICE_IDS = {("starter", "month"): "price_starter_m", ("pro", "month"): "price_pro_m",
                     ("crew", "month"): "price_crew_m"}
# A Billing-Portal upgrade: subscription item is now the CREW price, but the OLD checkout
# metadata still says "starter". The grant must follow the price (crew), not the metadata.
sub_upgraded = {"metadata": {"plan": "starter"},
                "items": {"data": [{"price": {"id": "price_crew_m"}}]}}
check("M2: _plan_from_subscription follows the billed price (crew), not stale metadata",
      billing._plan_from_subscription(sub_upgraded) == "crew")
check("M2: _confirmed_plan_for_price returns None on an unconfigured price (falls back)",
      billing._confirmed_plan_for_price("price_NOT_CONFIGURED") is None)
# When the price is unconfigured, metadata is the fallback.
sub_unconfigured = {"metadata": {"plan": "pro"},
                    "items": {"data": [{"price": {"id": "price_unknown"}}]}}
check("M2: an unconfigured price falls back to metadata",
      billing._plan_from_subscription(sub_unconfigured) == "pro")
# _on_invoice_paid resolution mirrors it: confirmed price wins.
inv = {"lines": {"data": [{"price": {"id": "price_crew_m"}}]},
       "subscription_details": {"metadata": {"plan": "starter"}}}
_grants = []
_o_grant = db.add_usage_grant
db.add_usage_grant = lambda business_id, **kw: _grants.append(kw.get("conversations_granted"))
_o_bid = billing._business_id_from_obj
billing._business_id_from_obj = lambda obj: bc
_o_ub = db.update_billing
db.update_billing = lambda *a, **k: None
billing._on_invoice_paid(inv)
db.add_usage_grant, billing._business_id_from_obj, db.update_billing = _o_grant, _o_bid, _o_ub
check("M2: invoice.paid grants the CREW allotment (3000), not starter (250)",
      _grants == [billing.PLAN_GRANTS["crew"]])
billing.PRICE_IDS = _orig_prices


# ===========================================================================
# D1 -- password-reset token is single-use
# ===========================================================================
print("\n=== D1: password-reset token single-use ===")
_u = db.create_user("d1@example.com", "hash", bc) if hasattr(db, "create_user") else None
_uid = _u if isinstance(_u, int) else (_u or {}).get("id") if _u else 1
db.create_password_reset_token(_uid, "tok_d1", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
check("D1: first redemption returns the user id", db.consume_password_reset_token("tok_d1") == _uid)
check("D1: a second redemption of the same token returns None (single-use)",
      db.consume_password_reset_token("tok_d1") is None)


# ===========================================================================
# L1 -- cancel_appointment also cancels the morning_reminder
# ===========================================================================
print("\n=== L1: cancel_appointment cancels morning_reminder ===")
bl = db.create_business({"name": "L1 Co"})
ll = db.create_lead(bl, "L1 Lead", "+15551110009")
db.book_appointment(bl, ll, "2026-07-02 14:00", day="2026-07-02", slot_time="14:00")
_c = db.get_conn()
_aid = _c.execute("SELECT id FROM appointments WHERE business_id=? AND lead_id=? AND status='booked'",
                  (bl, ll)).fetchone()[0]
_c.close()
_rid = db.add_scheduled_message(bl, ll, _aid, "reminder", _SA, "reminder")
_mid = db.add_scheduled_message(bl, ll, _aid, "morning_reminder", _SA, "morning reminder")
db.cancel_appointment(bl, _aid)
def _st(sid):
    c = db.get_conn(); r = c.execute("SELECT status FROM scheduled_messages WHERE id=?", (sid,)).fetchone(); c.close()
    return r[0] if r else None
check("L1: the booking reminder is canceled", _st(_rid) == "canceled")
check("L1: the morning_reminder is ALSO canceled (no orphan blocking the rebook)",
      _st(_mid) == "canceled")


# ===========================================================================
# A1 -- /api/appointments/<id>/cancel requires CSRF
# ===========================================================================
print("\n=== A1: appointment-cancel requires CSRF ===")
client = appmod.app.test_client()
client.post("/login", data={"email": config.SEED_OWNER_EMAIL, "password": config.SEED_OWNER_PASSWORD})
with client.session_transaction() as _s:
    _s["csrf_token"] = "test_csrf"
db.book_appointment(1, db.create_lead(1, "A1 Lead", "+15551110020"),
                    "2026-07-03 10:00", day="2026-07-03", slot_time="10:00")
_c = db.get_conn()
_a1 = _c.execute("SELECT id FROM appointments WHERE business_id=1 AND status='booked' "
                 "ORDER BY id DESC LIMIT 1").fetchone()[0]
_c.close()
_r_nocsrf = client.post(f"/api/appointments/{_a1}/cancel")
check("A1: cancel without _csrf -> 403", _r_nocsrf.status_code == 403)
check("A1: the appointment is still booked after the forged attempt",
      _st_a1 := (db.get_conn().execute("SELECT status FROM appointments WHERE id=?", (_a1,)).fetchone()[0]) == "booked")
_r_csrf = client.post(f"/api/appointments/{_a1}/cancel", data={"_csrf": "test_csrf"})
check("A1: cancel WITH _csrf -> 200", _r_csrf.status_code == 200)


print(f"\n{'='*46}")
print(f"Results: {_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
