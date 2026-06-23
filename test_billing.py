"""Billing tests (Phase 1 A). Run: python3 test_billing.py
Standalone script; prints ok/FAIL per test; exits 0 all-green, 1 on any failure.
All Stripe SDK calls are mocked — zero network, no Stripe account needed.
"""
import os
import sys
import tempfile
import types
import unittest.mock as mock

# ── Env setup (must happen before importing anything that reads env vars) ──────
os.environ.setdefault("FIRSTBACK_PROVIDER", "demo")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN",  "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://ringback-gixe.onrender.com")
os.environ.setdefault("FIRSTBACK_TASKS_SECRET", "tasks_secret_test")
# Fake Stripe keys so billing.py doesn't throw "not configured".
os.environ["STRIPE_SECRET_KEY"]     = "sk_test_fake"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_fake"
os.environ["STRIPE_PRICE_STARTER"]  = "price_starter_test"
os.environ["STRIPE_PRICE_PRO"]      = "price_pro_test"
os.environ["STRIPE_PRICE_CREW"]     = "price_crew_test"
os.environ["STRIPE_PRICE_STARTER_ANNUAL"] = "price_starter_year_test"
os.environ["STRIPE_PRICE_PRO_ANNUAL"]     = "price_pro_year_test"
os.environ["STRIPE_PRICE_CREW_ANNUAL"]    = "price_crew_year_test"

# ── Temp DB ────────────────────────────────────────────────────────────────────
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

# Block real network (same guard as test_setup.py).
import requests as _rq_guard
class _NetworkLeak(BaseException):
    pass
def _no_net(*a, **k):
    raise _NetworkLeak(f"unstubbed network call: {a[0] if a else '?'}")
_rq_guard.get  = _no_net
_rq_guard.post = _no_net

import billing
import compliance

# ── Test harness ───────────────────────────────────────────────────────────────
_pass = _fail = 0

def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        suffix = f" ({detail})" if detail else ""
        print(f"FAIL   {name}{suffix}")

# ── Stripe mock fixtures ───────────────────────────────────────────────────────
# Minimal Stripe-event dicts — copied from Stripe's sample-events docs.

def _checkout_session(business_id=1, plan="starter", customer="cus_test123",
                      sub_id="sub_test123"):
    return {
        "id": "cs_test_abc",
        "object": "checkout.session",
        "mode": "subscription",
        "customer": customer,
        "subscription": sub_id,
        "metadata": {"business_id": str(business_id), "plan": plan},
    }

def _subscription(business_id=1, plan="pro", status="active",
                  sub_id="sub_test123", price_id="price_pro_test"):
    return {
        "id": sub_id,
        "object": "subscription",
        "status": status,
        "customer": "cus_test123",
        "metadata": {"business_id": str(business_id), "plan": plan},
        "items": {
            "data": [{"price": {"id": price_id}}]
        },
        "current_period_end": 9999999999,
    }

def _invoice(business_id=1, plan="pro", sub_id="sub_test123",
             period_start=1700000000, period_end=1702000000):
    return {
        "id": "in_test123",
        "object": "invoice",
        "customer": "cus_test123",
        "subscription": sub_id,
        "subscription_details": {"metadata": {"business_id": str(business_id), "plan": plan}},
        "period_start": period_start,
        "period_end":   period_end,
        "lines": {
            "data": [{
                "price": {"id": "price_pro_test"},
                "period": {"start": period_start, "end": period_end},
            }]
        },
    }

def _event(event_id, event_type, obj):
    """Wrap a Stripe object into a minimal event envelope."""
    return {
        "id":   event_id,
        "type": event_type,
        "data": {"object": obj},
    }

# ── 1: DB migration leaves seed tenant (business_id=1) active ─────────────────
print("\n=== Migration guard ===")
db.init_db()
biz = db.get_business(1)
check("seed tenant subscription_status defaults to 'active' after migration",
      biz.get("subscription_status") == "active",
      detail=repr(biz.get("subscription_status")))

# ── 2: Checkout creates an active subscription ─────────────────────────────────
print("\n=== Checkout → active subscription ===")

def _fake_checkout_create(**kwargs):
    """Simulate stripe.checkout.Session.create returning a session dict."""
    meta = kwargs.get("metadata", {})
    return {
        "id": "cs_test_checkout",
        "url": "https://checkout.stripe.com/pay/cs_test_checkout",
        "customer": "cus_new",
        "subscription": "sub_new",
        "metadata": meta,
    }

with mock.patch("stripe.checkout.Session.create", side_effect=_fake_checkout_create):
    session = billing.create_checkout_session(1, "pro")
check("checkout session has a URL",
      "checkout.stripe.com" in (session.get("url") or ""))

# ── 3: Webhook — checkout.session.completed writes billing data ────────────────
print("\n=== Webhook: checkout.session.completed ===")

def _make_webhook_mock(event_dict):
    """Return a callable that makes stripe.Webhook.construct_event return event_dict."""
    def _construct(payload, sig, secret):
        return event_dict
    return _construct

checkout_event = _event("evt_checkout_001",
                         "checkout.session.completed",
                         _checkout_session(business_id=1, plan="pro"))

with mock.patch("stripe.Webhook.construct_event",
                side_effect=_make_webhook_mock(checkout_event)):
    msg, code = billing.handle_webhook(b"payload", "t=xxx,v1=yyy")

check("checkout webhook returns 200", code == 200)
biz = db.get_business(1)
check("checkout webhook stores stripe_customer_id",
      biz.get("stripe_customer_id") == "cus_test123")
check("checkout webhook sets subscription_status=active",
      biz.get("subscription_status") == "active")

# ── 4: Webhook idempotency — same event id twice = one effect ─────────────────
print("\n=== Webhook idempotency ===")

# First call.
with mock.patch("stripe.Webhook.construct_event",
                side_effect=_make_webhook_mock(checkout_event)):
    billing.handle_webhook(b"payload", "t=xxx,v1=yyy")  # already processed above

# Override billing fields with a sentinel, then re-send the same event.
conn = db.get_conn()
conn.execute("UPDATE businesses SET stripe_customer_id='sentinel' WHERE id=1")
conn.commit(); conn.close()

with mock.patch("stripe.Webhook.construct_event",
                side_effect=_make_webhook_mock(checkout_event)):
    msg2, code2 = billing.handle_webhook(b"payload", "t=xxx,v1=yyy")

check("duplicate event id returns 200", code2 == 200)
check("duplicate event does NOT overwrite data (idempotent)",
      msg2 == "already processed")
biz = db.get_business(1)
check("customer id unchanged after duplicate event",
      biz.get("stripe_customer_id") == "sentinel")

# Restore to cus_test123 for subsequent tests.
conn = db.get_conn()
conn.execute("UPDATE businesses SET stripe_customer_id='cus_test123' WHERE id=1")
conn.commit(); conn.close()

# ── 5: Webhook bad signature → exception ──────────────────────────────────────
print("\n=== Webhook: bad signature ===")

def _bad_sig(payload, sig, secret):
    import stripe
    raise stripe.error.SignatureVerificationError(
        "No signatures found matching expected signature", sig_header=sig)

bad_sig_event = _event("evt_badsig_001", "checkout.session.completed",
                        _checkout_session())
try:
    with mock.patch("stripe.Webhook.construct_event", side_effect=_bad_sig):
        billing.handle_webhook(b"tampered", "t=bad,v1=bad")
    check("bad signature raises an exception", False,
          detail="no exception was raised")
except Exception:
    check("bad signature raises an exception", True)

# ── 6: invoice.paid writes usage_grants ───────────────────────────────────────
print("\n=== invoice.paid → usage_grants ===")

# First, ensure the tenant's customer id is set (from test 3 above).
invoice_event = _event("evt_invoice_001", "invoice.paid",
                        _invoice(business_id=1, plan="pro"))

with mock.patch("stripe.Webhook.construct_event",
                side_effect=_make_webhook_mock(invoice_event)):
    billing.handle_webhook(b"payload", "t=xxx,v1=invoice")

grants = db.get_usage_grants(1)
check("invoice.paid created a usage_grant row", len(grants) >= 1)
if grants:
    check("usage_grant conversations_granted = 1000 (Pro plan)",
          grants[0]["conversations_granted"] == 1000,
          detail=repr(grants[0]["conversations_granted"]))
    check("usage_grant source contains 'stripe'",
          "stripe" in (grants[0]["source"] or ""))

# ── 7: invoice.paid is idempotent (same event id) ────────────────────────────
print("\n=== invoice.paid idempotency ===")
grants_before = db.get_usage_grants(1)

with mock.patch("stripe.Webhook.construct_event",
                side_effect=_make_webhook_mock(invoice_event)):
    billing.handle_webhook(b"payload", "t=xxx,v1=invoice")  # same event id

grants_after = db.get_usage_grants(1)
check("duplicate invoice.paid does not write a second grant",
      len(grants_after) == len(grants_before))

# ── 8: Cancelled tenant fails launch_blockers ─────────────────────────────────
print("\n=== Subscription gate: canceled tenant blocked ===")

db.update_billing(1, subscription_status="canceled")
biz = db.get_business(1)
blockers = compliance.launch_blockers(biz, sms_configured=True)
check("canceled subscription appears in launch_blockers",
      any("subscription" in b.lower() or "canceled" in b.lower() for b in blockers),
      detail=str(blockers))

# ── 9: Active tenant is not blocked by subscription gate ─────────────────────
print("\n=== Subscription gate: active tenant not blocked ===")

db.update_billing(1, subscription_status="active")
biz = db.get_business(1)
# Give it all the other prerequisites too so the only blockers are infra, not billing.
# We just check the subscription-specific blocker is absent.
blockers = compliance.launch_blockers(biz, sms_configured=True)
sub_blocked = any("subscription" in b.lower() or "canceled" in b.lower()
                  for b in blockers)
check("active subscription does not appear in launch_blockers", not sub_blocked,
      detail=str(blockers))

# ── 10: past_due tenant is also blocked ───────────────────────────────────────
print("\n=== Subscription gate: past_due tenant blocked ===")

db.update_billing(1, subscription_status="past_due")
biz = db.get_business(1)
blockers = compliance.launch_blockers(biz, sms_configured=True)
check("past_due subscription appears in launch_blockers",
      any("subscription" in b.lower() or "past_due" in b.lower() for b in blockers),
      detail=str(blockers))

# ── 11: Customer portal session mock ──────────────────────────────────────────
print("\n=== Customer portal ===")

def _fake_portal_create(customer, return_url):
    return {"url": f"https://billing.stripe.com/portal/{customer}"}

db.update_billing(1, subscription_status="active",
                  stripe_customer_id="cus_test123")

with mock.patch("stripe.billing_portal.Session.create",
                side_effect=_fake_portal_create):
    portal = billing.create_portal_session(1)

check("portal session has a URL",
      "billing.stripe.com" in (portal.get("url") or ""))

# ── 12: Portal raises ValueError when no customer id ──────────────────────────
print("\n=== Portal: no customer id raises ValueError ===")

db.update_billing(2, stripe_customer_id=None)  # make sure biz 2 has no customer
# Create biz 2 if it doesn't exist.
conn = db.get_conn()
if not conn.execute("SELECT 1 FROM businesses WHERE id=2").fetchone():
    conn.execute("INSERT INTO businesses (id, name, subscription_status) "
                 "VALUES (2, 'Test Biz 2', 'active')")
    conn.commit()
conn.close()

try:
    billing.create_portal_session(2)
    check("portal raises ValueError for no customer", False, detail="no exception")
except ValueError:
    check("portal raises ValueError for no customer", True)

# ── 14: Annual (20%-off) checkout ──────────────────────────────────────────────
print("\n=== Annual (20% off) checkout ===")
_cap = {}
def _capture_checkout(**kwargs):
    _cap.clear(); _cap.update(kwargs)
    return {"id": "cs_year", "url": "https://checkout.stripe.com/pay/cs_year"}

with mock.patch("stripe.checkout.Session.create", side_effect=_capture_checkout):
    billing.create_checkout_session(1, "pro", interval="year")
check("annual checkout uses the ANNUAL price id",
      _cap.get("line_items", [{}])[0].get("price") == "price_pro_year_test",
      detail=repr(_cap.get("line_items")))
check("annual checkout records interval=year in metadata",
      _cap.get("metadata", {}).get("interval") == "year")

with mock.patch("stripe.checkout.Session.create", side_effect=_capture_checkout):
    billing.create_checkout_session(1, "starter")  # default → month
check("default checkout uses the MONTHLY price id",
      _cap.get("line_items", [{}])[0].get("price") == "price_starter_test")
check("annual allotment == monthly allotment (gauge refills monthly, not 12x)",
      billing.PLAN_GRANTS["pro"] == 1000)

# ── 13: Starter and Crew plan grant amounts ────────────────────────────────────
print("\n=== Plan → grant amounts ===")

# Starter: 250
starter_invoice = _event("evt_invoice_starter", "invoice.paid",
                          _invoice(business_id=1, plan="starter",
                                   period_start=1800000000, period_end=1802000000))
starter_invoice["data"]["object"]["lines"]["data"][0]["price"]["id"] = "price_starter_test"
starter_invoice["data"]["object"]["subscription_details"]["metadata"]["plan"] = "starter"

grants_before = db.get_usage_grants(1)
with mock.patch("stripe.Webhook.construct_event",
                side_effect=_make_webhook_mock(starter_invoice)):
    billing.handle_webhook(b"payload", "t=xxx,v1=starter")

grants_after = db.get_usage_grants(1)
new_grants = [g for g in grants_after if g not in grants_before]
check("starter invoice.paid grants 250 conversations",
      any(g["conversations_granted"] == 250 for g in new_grants),
      detail=str([g["conversations_granted"] for g in new_grants]))

# Crew: 3000
crew_invoice = _event("evt_invoice_crew", "invoice.paid",
                       _invoice(business_id=1, plan="crew",
                                period_start=1900000000, period_end=1902000000))
crew_invoice["data"]["object"]["lines"]["data"][0]["price"]["id"] = "price_crew_test"
crew_invoice["data"]["object"]["subscription_details"]["metadata"]["plan"] = "crew"

grants_before = db.get_usage_grants(1)
with mock.patch("stripe.Webhook.construct_event",
                side_effect=_make_webhook_mock(crew_invoice)):
    billing.handle_webhook(b"payload", "t=xxx,v1=crew")

grants_after = db.get_usage_grants(1)
new_grants = [g for g in grants_after if g not in grants_before]
check("crew invoice.paid grants 3000 conversations",
      any(g["conversations_granted"] == 3000 for g in new_grants),
      detail=str([g["conversations_granted"] for g in new_grants]))

# ── 14: Compliance.subscription_active helper ─────────────────────────────────
print("\n=== compliance.subscription_active helper ===")
check("active → subscription_active True",
      compliance.subscription_active({"subscription_status": "active"}))
check("NULL/missing → subscription_active True (legacy/seed tenant)",
      compliance.subscription_active({"subscription_status": None}))
check("canceled → subscription_active False",
      not compliance.subscription_active({"subscription_status": "canceled"}))
check("past_due → subscription_active False",
      not compliance.subscription_active({"subscription_status": "past_due"}))

# ── 15: Phase 6a D-2 — unrecognized price_id is LOUD, not a silent downgrade ───
print("\n=== D-2: unrecognized price_id fail-loud ===")
import io as _io
import contextlib as _ctx

# A recognized price still maps cleanly with no noise.
_buf = _io.StringIO()
with mock.patch.object(billing.mail, "send_email") as _m_ok, _ctx.redirect_stderr(_buf):
    _plan_ok = billing._price_to_plan("price_pro_test")
check("D-2 recognized price_id maps to its plan", _plan_ok == "pro")
check("D-2 recognized price_id emits no BILLING WARNING", "BILLING WARNING" not in _buf.getvalue())
check("D-2 recognized price_id does not email the operator", not _m_ok.called)

# A real-but-unconfigured price_id: still grant starter (safe) BUT warn loudly + email.
_buf2 = _io.StringIO()
with mock.patch.object(billing.mail, "send_email") as _m_bad, _ctx.redirect_stderr(_buf2):
    _plan_bad = billing._price_to_plan("price_LIVE_BUT_UNCONFIGURED_999")
    import time as _t
    _t.sleep(0.2)   # let the daemon email thread run
check("D-2 unrecognized price_id still grants starter (safe fallback)", _plan_bad == "starter")
check("D-2 unrecognized price_id emits a BILLING WARNING to stderr",
      "BILLING WARNING" in _buf2.getvalue() and "price_LIVE_BUT_UNCONFIGURED_999" in _buf2.getvalue())
check("D-2 unrecognized price_id emails the operator (best-effort)", _m_bad.called)

# An empty price_id is the caller-filtered case → quiet starter, no false alarm.
_buf3 = _io.StringIO()
with mock.patch.object(billing.mail, "send_email") as _m_empty, _ctx.redirect_stderr(_buf3):
    _plan_empty = billing._price_to_plan("")
check("D-2 empty price_id is quiet (no warning, no email)",
      _plan_empty == "starter" and "BILLING WARNING" not in _buf3.getvalue() and not _m_empty.called)

# ── Pricing wiring: configured() gate + web-base URL (bug fix) ───────────────────
check("configured() True when secret key + 3 monthly price IDs set", billing.configured() is True)
check("_web_base() targets the FLASK web app, not the voice host",
      billing._web_base() == "https://ringback-gixe.onrender.com")
_saved_key = billing.STRIPE_SECRET_KEY
billing.STRIPE_SECRET_KEY = ""
check("configured() False when STRIPE_SECRET_KEY missing (subscribe UI stays gated)",
      billing.configured() is False)
billing.STRIPE_SECRET_KEY = _saved_key


# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"Results: {_pass} passed, {_fail} failed")
if _fail:
    sys.exit(1)
sys.exit(0)
