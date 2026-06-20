"""Batch E (slice 1) -- Customer Book page (plan 07-2). Run: python3 test_customer_book.py

The database rendered as a visible, switching-cost asset:
  - db.customer_book_stats: tenant-scoped totals, repeat (2+ jobs), total jobs, top 5.
  - /customers is now an authenticated app page (was a public marketing route).
  - The marketing "customer stories" page moved to /resources/customer-stories.
"""
import os
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""

import app as appmod
client = appmod.app.test_client()

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# --- empty book ---
s = db.customer_book_stats(1)
check("empty book -> all zeros",
      s == {"total_customers": 0, "repeat_customers": 0, "total_jobs": 0, "top_customers": []})

# --- seed: 3 booked customers, one a repeat (2 jobs) => total_jobs 4, repeat 1 ---
a = db.create_lead(1, "Repeat Rita", "+15550000001")
db.book_appointment(1, a, "2026-05-01 10:00", day="2026-05-01", slot_time="10:00")
db.book_appointment(1, a, "2026-06-01 10:00", day="2026-06-01", slot_time="10:00")
b = db.create_lead(1, "One-Job Joe", "+15550000002")
db.book_appointment(1, b, "2026-05-10 09:00", day="2026-05-10", slot_time="09:00")
c = db.create_lead(1, "Single Sam", "+15550000003")
db.book_appointment(1, c, "2026-05-20 11:00", day="2026-05-20", slot_time="11:00")
# a never-booked lead must NOT appear in the book
db.create_lead(1, "Unbooked Una", "+15550000009")

s = db.customer_book_stats(1)
check("counts only booked customers", s["total_customers"] == 3)
check("repeat = customers with 2+ jobs", s["repeat_customers"] == 1)
check("total_jobs sums all booked appointments", s["total_jobs"] == 4)
check("top_customers sorted by job_count desc", s["top_customers"][0]["name"] == "Repeat Rita"
      and s["top_customers"][0]["job_count"] == 2)
check("last_job_day is the most recent", s["top_customers"][0]["last_job_day"] == "2026-06-01")

# --- tenant isolation: a second business sees its own (empty) book ---
bid2 = db.create_business({"name": "Other Co"})
check("customer book is tenant-scoped", db.customer_book_stats(bid2)["total_customers"] == 0)

# --- routing ---
check("/customers requires login (302 when anonymous)", client.get("/customers").status_code == 302)
check("marketing stories moved to /resources/customer-stories (public 200)",
      client.get("/resources/customer-stories").status_code == 200)

client.post("/login", data={"email": config.SEED_OWNER_EMAIL, "password": config.SEED_OWNER_PASSWORD})
r = client.get("/customers")
h = r.get_data(as_text=True)
check("/customers renders the book when authed", r.status_code == 200 and "Customer book" in h)
check("book shows a real customer + repeat tile", "Repeat Rita" in h and "Repeat customers" in h)
check("book shows lifetime job money", "~$" in h)
check("book phone numbers are tel: links", 'href="tel:+15550000001"' in h)
# Honest money: with no owner-set avg_job_value, the lifetime $ is labeled a trade estimate.
check("lifetime money labeled a trade estimate when avg unset", "Trade estimate" in h)
db.set_avg_job_value(1, 1500)
h2 = client.get("/customers").get_data(as_text=True)
check("with a real avg set, shows the per-job rate (not 'estimate')",
      "At ~$1,500/job" in h2 and "Trade estimate" not in h2)

# --- marketing templates point to the new public URL; only the app shell nav may link
#     to the authed /customers book ---
import glob
_linkers = []
for f in glob.glob("templates/*.html"):
    with open(f) as fh:
        if 'href="/customers"' in fh.read():
            _linkers.append(os.path.basename(f))
check("marketing templates no longer link to /customers (only the app shell nav does)",
      _linkers == ["app_shell.html"])
for mk in ("marketing_base.html", "onboarding.html", "resources.html"):
    with open(f"templates/{mk}") as fh:
        body = fh.read()
    check(f"{mk} points to /resources/customer-stories",
          'href="/resources/customer-stories"' in body and 'href="/customers"' not in body)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
