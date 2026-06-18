"""Public /demo + sandbox-business isolation checks.  Run: python3 test_demo_public.py

Proves:
  1. GET /demo returns 200 with NO login (unauthenticated).
  2. POST /api/demo/incoming returns a lead scoped to the sandbox business only —
     NOT to any real tenant (business 1).
  3. POST /api/demo/reply processes a reply and keeps it inside the sandbox only.
  4. The logged-in /api/sim/* endpoints are still protected by @login_required.

Throwaway temp DB + the deterministic demo brain; no network.
"""
import os
import sys
import tempfile
import json

os.environ["FIRSTBACK_PROVIDER"] = "demo"   # deterministic, no network
import config

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""   # configured() False -> sends simulate

import app as _app
client = _app.app.test_client()

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ====================== 1. /demo accessible without login ======================
print("\n-- /demo public access --")
r = client.get("/demo")
check("/demo returns 200 without any login", r.status_code == 200)
html = r.get_data(as_text=True)
check("/demo renders the simulator template (has sim-2col)",
      "sim-2col" in html)
check("/demo template points to /api/demo/incoming (sandbox path)",
      "/api/demo/incoming" in html)
check("/demo template points to /api/demo/reply (sandbox path)",
      "/api/demo/reply" in html)

# ====================== 2. /api/demo/incoming scoped to sandbox only ======================
print("\n-- /api/demo/incoming sandbox isolation --")
r2 = client.post(
    "/api/demo/incoming",
    data=json.dumps({"name": "Test Caller", "phone": "+1 (555) 000-9999", "scenario": "prospect"}),
    content_type="application/json",
)
check("/api/demo/incoming returns 200 without login", r2.status_code == 200)
data2 = r2.get_json()
check("/api/demo/incoming returns a lead_id", "lead_id" in data2)
check("/api/demo/incoming returns a reply string", isinstance(data2.get("reply"), str))
check("/api/demo/incoming tags response as demo",
      data2.get("_demo") is True)

demo_lead_id = data2.get("lead_id")
demo_biz_id = data2.get("_biz_id")

# Confirm the lead does NOT belong to real business 1
lead_in_sandbox = db.get_lead(demo_lead_id, demo_biz_id)
lead_in_real_biz = db.get_lead(demo_lead_id, 1)   # must NOT be visible here

check("demo lead exists in the sandbox business",
      lead_in_sandbox is not None)
check("demo lead is NOT visible under real business 1 (isolation guard)",
      lead_in_real_biz is None)
check("sandbox business is NOT the real business 1 (distinct IDs)",
      demo_biz_id is not None and demo_biz_id != 1)

# Confirm the sandbox business name sentinel is correct internally
_sandbox_conn = db.get_conn()
_sandbox_row = _sandbox_conn.execute(
    "SELECT name FROM businesses WHERE id=?", (demo_biz_id,)
).fetchone()
_sandbox_conn.close()
check("sandbox business is identified by the sentinel name",
      _sandbox_row is not None and _sandbox_row[0] == _app._DEMO_BIZ_SENTINEL)

# ====================== 3. /api/demo/reply scoped to sandbox only ======================
print("\n-- /api/demo/reply sandbox isolation --")
r3 = client.post(
    "/api/demo/reply",
    data=json.dumps({"lead_id": demo_lead_id, "body": "Hi, I need a quote for painting."}),
    content_type="application/json",
)
check("/api/demo/reply returns 200 without login", r3.status_code == 200)
data3 = r3.get_json()
check("/api/demo/reply returns a reply string",
      isinstance(data3.get("reply"), str) and len(data3["reply"]) > 0)
check("/api/demo/reply tags response as demo",
      data3.get("_demo") is True)

# Try to reply to a lead_id that belongs to real biz 1 — must 404
real_lead_id = db.create_lead(1, "Real Tenant Caller", "+1 (555) 111-2222")
r3b = client.post(
    "/api/demo/reply",
    data=json.dumps({"lead_id": real_lead_id, "body": "trying to cross tenant"}),
    content_type="application/json",
)
check("demo /reply rejects a real-tenant lead_id (cross-tenant isolation)",
      r3b.status_code == 404)

# ====================== 4. Logged-in sim endpoints still protected ======================
print("\n-- /api/sim/* still requires login --")
r4 = client.post(
    "/api/sim/incoming",
    data=json.dumps({"scenario": "prospect"}),
    content_type="application/json",
)
check("/api/sim/incoming redirects/403 when not logged in",
      r4.status_code in (302, 401, 403))

r4b = client.post(
    "/api/sim/reply",
    data=json.dumps({"lead_id": 1, "body": "hi"}),
    content_type="application/json",
)
check("/api/sim/reply redirects/403 when not logged in",
      r4b.status_code in (302, 401, 403))

# ====================== 5. Spam/known scenarios on public demo ======================
print("\n-- /api/demo/incoming spam + known scenarios --")
r5a = client.post(
    "/api/demo/incoming",
    data=json.dumps({"scenario": "spam"}),
    content_type="application/json",
)
check("demo spam scenario returns screened=True",
      r5a.status_code == 200 and r5a.get_json().get("screened") is True)

r5b = client.post(
    "/api/demo/incoming",
    data=json.dumps({"scenario": "known"}),
    content_type="application/json",
)
check("demo known scenario returns screened=True",
      r5b.status_code == 200 and r5b.get_json().get("screened") is True)

# ====================== Summary ======================
print(f"\n{'='*50}")
print(f"  {_pass} passed  /  {_fail} failed")
if _fail:
    sys.exit(1)
sys.exit(0)
