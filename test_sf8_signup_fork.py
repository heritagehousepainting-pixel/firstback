"""SF-8 signup EIN fork + setup_a2p mode=auto tests.  Run: python3 test_sf8_signup_fork.py

Covers:
  1. POST /signup with has_ein present -> db.set_business_type called with "llc"
  2. POST /signup without has_ein -> db.set_business_type called with "sole_prop"
  3. POST /setup/a2p mode=auto calls connections.submit_a2p (patched; profile complete)
  4. POST /setup/a2p mode=auto with profile incomplete -> redirects ?err=profile
  5. POST /setup/a2p mode=record (operator path) still works unchanged
  6. POST /setup/a2p mode=auto with submit_a2p returning error -> redirects ?err=a2p_submit
  7. POST /setup/a2p with no mode field -> defaults to auto path

Stubs:
  - db.set_business_type (A1 seam)
  - connections.submit_a2p (A2 seam)
  - connections.profile_complete (A2 seam -- we control it per-test)
"""
import os
import sys
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")
os.environ.setdefault("FIRSTBACK_PUBLIC_URL", "https://ringback-gixe.onrender.com")
# Operator email so mode=record tests can pass the operator check
os.environ.setdefault("FIRSTBACK_OPERATOR_EMAILS", "heritagehousepainting@gmail.com")

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

# --- Stub A1 seam: db.set_business_type ---
_set_business_type_calls = []

def _stub_set_business_type(business_id, business_type):
    _set_business_type_calls.append((business_id, business_type))
    # Also write to DB if the column exists (best-effort)
    try:
        conn = db.get_conn()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
        if "business_type" not in cols:
            conn.execute("ALTER TABLE businesses ADD COLUMN business_type TEXT DEFAULT 'unknown'")
        conn.execute("UPDATE businesses SET business_type=? WHERE id=?",
                     (business_type, business_id))
        conn.commit()
        conn.close()
    except Exception:
        pass

db.set_business_type = _stub_set_business_type

# --- Stub A2 seam: connections.submit_a2p ---
import connections
_submit_a2p_calls = []
_submit_a2p_return = [{"status": "simulated"}]  # mutable container

def _stub_submit_a2p(business_id):
    _submit_a2p_calls.append(business_id)
    return _submit_a2p_return[0]

connections.submit_a2p = _stub_submit_a2p

# --- Stub A2 seam: connections.profile_complete ---
_profile_complete_flag = [True]  # mutable so tests can toggle

def _stub_profile_complete(biz):
    return _profile_complete_flag[0]

connections.profile_complete = _stub_profile_complete

# Ensure extra columns exist for microsite route compatibility
def _ensure_extra_cols():
    conn = db.get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(businesses)").fetchall()]
    for col, ddl in (
        ("micro_site_slug", "TEXT"),
        ("legal_business_name", "TEXT"),
        ("business_address", "TEXT"),
    ):
        if col not in cols:
            conn.execute("ALTER TABLE businesses ADD COLUMN %s %s" % (col, ddl))
    conn.commit()
    conn.close()

_ensure_extra_cols()

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


def login():
    return client.post("/login", data={
        "email": config.SEED_OWNER_EMAIL,
        "password": config.SEED_OWNER_PASSWORD
    })


# ====================== 1. Signup with has_ein -> "llc" ======================
print("\n-- signup: has_ein -> llc --")
_set_business_type_calls.clear()
r = client.post("/signup", data={
    "email": "llc_owner@example.com",
    "password": "password123",
    "business": "LLC Test Co",
    "owner": "Test Owner",
    "has_ein": "1",
})
check("signup with has_ein redirects", r.status_code in (302, 303))
check("set_business_type was called", len(_set_business_type_calls) >= 1)
if _set_business_type_calls:
    check("set_business_type called with 'llc'", _set_business_type_calls[-1][1] == "llc")
else:
    check("set_business_type called with 'llc'", False)


# ====================== 2. Signup without has_ein -> "sole_prop" ======================
print("\n-- signup: no has_ein -> sole_prop --")
_set_business_type_calls.clear()
# Clear session: previous signup left uid in session; signup route redirects early when logged in
with client.session_transaction() as _sess:
    _sess.clear()
r2 = client.post("/signup", data={
    "email": "soleprop@example.com",
    "password": "password456",
    "business": "Sole Prop Painting",
    "owner": "Dave",
    # has_ein NOT included
})
check("signup without has_ein redirects", r2.status_code in (302, 303))
check("set_business_type was called", len(_set_business_type_calls) >= 1)
if _set_business_type_calls:
    check("set_business_type called with 'sole_prop'", _set_business_type_calls[-1][1] == "sole_prop")
else:
    check("set_business_type called with 'sole_prop'", False)


# ====================== 3. setup_a2p mode=auto calls submit_a2p ======================
print("\n-- setup_a2p mode=auto calls connections.submit_a2p --")
# Log in as the seed owner (operator) for all setup_a2p tests.
with client.session_transaction() as _sess:
    _sess.clear()
login()
_submit_a2p_calls.clear()
_profile_complete_flag[0] = True
_submit_a2p_return[0] = {"status": "simulated"}

r3 = client.post("/setup/a2p", data={"mode": "auto"})
check("mode=auto redirects (not 4xx/5xx)", r3.status_code in (302, 303))
check("mode=auto calls submit_a2p", len(_submit_a2p_calls) >= 1)
loc3 = r3.headers.get("Location", "")
check("mode=auto on success redirects to ?saved=a2p", "saved=a2p" in loc3)


# ====================== 4. setup_a2p mode=auto, profile incomplete ======================
print("\n-- setup_a2p mode=auto, profile incomplete -> err=profile --")
_submit_a2p_calls.clear()
_profile_complete_flag[0] = False

r4 = client.post("/setup/a2p", data={"mode": "auto"})
check("incomplete profile redirects", r4.status_code in (302, 303))
loc4 = r4.headers.get("Location", "")
check("incomplete profile -> ?err=profile", "err=profile" in loc4)
check("submit_a2p NOT called when profile incomplete", len(_submit_a2p_calls) == 0)


# ====================== 5. setup_a2p mode=auto, submit returns error ======================
print("\n-- setup_a2p mode=auto, submit_a2p returns error --")
_submit_a2p_calls.clear()
_profile_complete_flag[0] = True
_submit_a2p_return[0] = {"status": "error", "step": "brand"}

r5 = client.post("/setup/a2p", data={"mode": "auto"})
check("error result redirects", r5.status_code in (302, 303))
loc5 = r5.headers.get("Location", "")
check("error result -> ?err=a2p_submit", "err=a2p_submit" in loc5)


# ====================== 6. setup_a2p mode=record (operator path unchanged) ======================
print("\n-- setup_a2p mode=record (operator path unchanged) --")
_submit_a2p_calls.clear()
_submit_a2p_return[0] = {"status": "simulated"}
# Must be logged in as the operator (seed owner).
with client.session_transaction() as _sess:
    _sess.clear()
login()

# Stub connections.a2p_sync: the record path calls it, which in turn calls
# messaging.fetch_a2p_campaign_status -> real Twilio GET. Stub it out.
_orig_a2p_sync = connections.a2p_sync
connections.a2p_sync = lambda bid: None

r6 = client.post("/setup/a2p", data={
    "mode": "record",
    "brand_sid": "BNtest",
    "messaging_service_sid": "MGtest",
    "campaign_sid": "CMtest",
})
connections.a2p_sync = _orig_a2p_sync  # restore
check("mode=record redirects (not 4xx/5xx)", r6.status_code in (302, 303))
check("mode=record does NOT call submit_a2p", len(_submit_a2p_calls) == 0)


# ====================== 7. Default mode=auto when mode not posted ======================
print("\n-- setup_a2p default mode (no mode field) -> auto path --")
_submit_a2p_calls.clear()
_profile_complete_flag[0] = True
_submit_a2p_return[0] = {"status": "simulated"}

r7 = client.post("/setup/a2p", data={})  # no mode field
check("no mode field redirects", r7.status_code in (302, 303))
check("no mode field calls submit_a2p (defaults to auto)", len(_submit_a2p_calls) >= 1)


# ====================== Final tally ======================
print("\n" + "=" * 40)
print("  %d passed  %d failed  (%d total)" % (_pass, _fail, _pass + _fail))
if _fail:
    sys.exit(1)
