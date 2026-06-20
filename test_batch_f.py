"""Batch F -- pricing/marketing/SEO non-decision parts (plan 09). Run: python3 test_batch_f.py

Locks the BUILD-NOW marketing changes (the 3 founder-gated items stay out):
  - SEO/OG meta on the live homepage + pricing/solutions/customers.
  - "conversations" -> "missed-call replies" rename in pricing tiers.
  - ROI anchor strip, Pro extra-number add-on, soft-overage FAQ.
  - Customer "be the first case study" waitlist card; /webinars de-linked from nav.
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


def body(path):
    return client.get(path).get_data(as_text=True)


# --- SEO/OG meta on the live homepage (onboarding.html) + key marketing pages ---
home = body("/")
check("homepage has a meta description", 'name="description"' in home)
check("homepage has og:title", 'property="og:title"' in home)
check("homepage has twitter:card", 'name="twitter:card"' in home)
for path in ("/pricing", "/solutions", "/resources/customer-stories"):
    h = body(path)
    check(f"{path} has description + og:title", 'name="description"' in h and 'property="og:title"' in h)
# og:image is intentionally omitted until the asset exists (no silent 404)
check("no og:image points at the missing default asset", "/static/og-default.png" not in body("/pricing"))

# --- pricing: rename + ROI anchor + add-on + soft overage ---
p = body("/pricing")
check("pricing renamed conversations -> missed-call replies", p.count("missed-call replies") >= 3)
check("pricing has no 'conversations / mo' left in tiers", "conversations / mo" not in p)
check("pricing ROI anchor strip present ($45K)", "45K" in p and "answering service" in p)
check("pricing ROI footnote present (cites the math)", "average job value" in p)
check("Pro extra-number add-on surfaced (routes to /contact)", "$20/mo" in p and "/contact" in p)
check("overage FAQ uses the soft version (no unbuilt $0.75 promise)",
      "cut you off" in p and "$0.75" not in p)

# --- customers waitlist + nav de-link ---
cust = body("/resources/customer-stories")
check("customers: waitlist 'be the first case study' card", "Be the first case study" in cust)
check("customers: no harmful 'your quote goes here' placeholder", "quote goes here" not in cust)
check("nav: /webinars de-linked (coming-soon dead end)", 'href="/webinars"' not in p)
check("nav: customer-stories still linked", 'href="/resources/customer-stories"' in p)

# --- gate: pricing stays coming-soon (no live checkout exposed) ---
check("pricing CTAs go to /signup or /contact (no /billing/checkout link)",
      "/billing/checkout" not in p)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
