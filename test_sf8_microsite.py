"""SF-8 microsite + privacy tests.  Run: python3 test_sf8_microsite.py

Covers:
  1. GET /c/<known-slug> -> 200 + legal business name in HTML + "Reply STOP" present
     + no "Twilio" or "A2P" in rendered HTML + /privacy link present
  2. GET /c/<unknown-slug> -> 404
  3. privacy.html contains the new Text messaging / SMS section

Stubs A1 seam: db.set_business_type (needed by app import so signup route doesn't
crash -- but the microsite route only needs db.get_conn which is always present).
"""
import os
import sys
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://ringback-gixe.onrender.com")

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

# --- Stub A1 seam: db.set_business_type (not yet in db.py) ---
def _stub_set_business_type(business_id, business_type):
    """Stub: write business_type into the DB if the column exists, else no-op."""
    try:
        conn = db.get_conn()
        conn.execute(
            "UPDATE businesses SET business_type=? WHERE id=?",
            (business_type, business_id)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

db.set_business_type = _stub_set_business_type

# --- Stub A2 seam: connections.submit_a2p (not yet in connections.py) ---
import connections
_submit_a2p_calls = []
def _stub_submit_a2p(business_id):
    _submit_a2p_calls.append(business_id)
    return {"status": "simulated"}
connections.submit_a2p = _stub_submit_a2p

import app as _app
client = _app.app.test_client()

# Block real network
import requests as _rq
class _NetworkLeak(BaseException): pass
def _no_net(*a, **k):
    raise _NetworkLeak("unstubbed network call")
_rq.get = _no_net
_rq.post = _no_net

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("  ok   " + name)
    else:
        _fail += 1
        print("FAIL   " + name)


# --- Seed a business with a micro_site_slug ---
# The slug column comes from the Phase 3 migration (owned by A1).
# We add it here if not present so the test is standalone.
def _ensure_slug_column():
    conn = db.get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
    for col, ddl in (
        ("micro_site_slug", "TEXT"),
        ("legal_business_name", "TEXT"),
        ("business_address", "TEXT"),
        ("business_type", "TEXT DEFAULT 'unknown'"),
    ):
        if col not in cols:
            conn.execute("ALTER TABLE businesses ADD COLUMN %s %s" % (col, ddl))
    conn.commit()
    conn.close()

_ensure_slug_column()

TEST_SLUG = "heritage-house-painting-1"
TEST_LEGAL = "Heritage House Painting LLC"
TEST_ADDRESS = "123 Main St, Ambler PA 19002"

conn = db.get_conn()
conn.execute(
    "UPDATE businesses SET micro_site_slug=?, legal_business_name=?, business_address=? WHERE id=1",
    (TEST_SLUG, TEST_LEGAL, TEST_ADDRESS)
)
conn.commit()
conn.close()


# ====================== 1. Known slug -> 200 with required content ======================
print("\n-- /c/<slug> known slug --")
r = client.get("/c/" + TEST_SLUG)
check("/c/<slug> returns 200", r.status_code == 200)
html = r.get_data(as_text=True)
check("legal business name in rendered HTML", TEST_LEGAL in html)
check("Reply STOP present in HTML", "Reply STOP" in html)
check("no 'Twilio' jargon in rendered HTML", "Twilio" not in html)
check("no 'A2P' jargon in rendered HTML", "A2P" not in html)
check("/privacy link present", "/privacy" in html)
check("/terms link present", "/terms" in html)
check("opt-out copy: message and data rates", "Message and data rates may apply" in html)
check("no 'FirstBack' brand on microsite", "FirstBack" not in html)

# Smart/curly quote checks using unicode escapes to avoid editor corruption
LDQ = u"“"   # left double quote
RDQ = u"”"   # right double quote
LSQ = u"‘"   # left single quote
RSQ = u"’"   # right single quote
check("no curly left double quote in HTML", LDQ not in html)
check("no curly right double quote in HTML", RDQ not in html)
check("no curly left single quote in HTML", LSQ not in html)
check("no curly right single quote in HTML", RSQ not in html)


# ====================== 2. Unknown slug -> 404 ======================
print("\n-- /c/<slug> unknown slug --")
r2 = client.get("/c/no-such-business-xyz999")
check("unknown slug returns 404", r2.status_code == 404)


# ====================== 3. privacy.html has Text messaging section ======================
print("\n-- privacy.html Text messaging section --")
r3 = client.get("/privacy")
check("GET /privacy returns 200", r3.status_code == 200)
priv_html = r3.get_data(as_text=True)
check("privacy.html has 'Text messaging' heading", "Text messaging" in priv_html)
check("privacy.html has STOP opt-out copy", "STOP" in priv_html)
check("privacy.html has no-share mobile opt-in copy",
      "do not share mobile opt-in data" in priv_html or
      "not share mobile opt-in data" in priv_html)

# The new Text messaging section must use straight ASCII quotes only.
# Extract just that section to verify (the rest of the existing file is pre-existing).
import re as _re
tm_match = _re.search(r'Text messaging.*?(?=<h2|</div)', priv_html, _re.DOTALL)
if tm_match:
    tm_section = tm_match.group(0)
    check("new Text messaging section has no smart/curly quotes",
          LDQ not in tm_section and RDQ not in tm_section and
          LSQ not in tm_section and RSQ not in tm_section)
else:
    check("new Text messaging section found for quote check", False)


# ====================== Final tally ======================
print("\n" + "=" * 40)
print("  %d passed  %d failed  (%d total)" % (_pass, _fail, _pass + _fail))
if _fail:
    sys.exit(1)
