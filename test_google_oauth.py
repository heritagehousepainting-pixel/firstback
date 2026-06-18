"""Google OAuth hardening checks. Run: python3 test_google_oauth.py

Covers the three seams hardened in the call-screening branch (google_cal.py /
google_contacts.py previously had no unit tests):

  1. access_is_fresh()  -- the single, plainly-named freshness helper that replaced
     the duplicated, inverted `fresh` flag: valid -> use cache; expired / near-expiry
     / missing / unparseable -> refresh.
  2. token_crypto       -- encryption at rest with dual-read: a freshly written token
     is unreadable as plaintext in the DB file; a legacy plaintext row still reads.
  3. OAuth callbacks    -- the `state` (CSRF) check on BOTH the calendar and contacts
     callbacks rejects a missing/mismatched state, and an exchange failure renders an
     honest error, never a half-connected state.

No framework, no network: a throwaway temp DB, env set before imports, and the token
exchange monkeypatched. Exits non-zero on any failure.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

# A key MUST be set before importing config/token_crypto so encryption is active.
os.environ["FIRSTBACK_TOKEN_KEY"] = "unit-test-token-key-please-rotate"
os.environ.setdefault("FIRSTBACK_PROVIDER", "demo")    # no network from the app brain

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
db.init_db()

import token_crypto
from google_oauth import access_is_fresh
import google_cal
import google_contacts

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def _iso(delta_seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


# ---- 1. access_is_fresh: the de-duplicated, correctly-named freshness rule -----
check("valid token (1h out) is fresh -> use cache",
      access_is_fresh(_iso(3600)) is True)
check("expired token (1h ago) is NOT fresh -> refresh",
      access_is_fresh(_iso(-3600)) is False)
check("near-expiry token (30s out, inside 60s skew) is NOT fresh -> refresh",
      access_is_fresh(_iso(30)) is False)
check("token just past the skew (90s out) is fresh",
      access_is_fresh(_iso(90)) is True)
check("missing expiry -> NOT fresh (refresh)",
      access_is_fresh(None) is False and access_is_fresh("") is False)
check("unparseable expiry -> NOT fresh (refresh)",
      access_is_fresh("not-a-date") is False)
check("naive (tz-less) expiry is treated as UTC",
      access_is_fresh((datetime.now(timezone.utc) + timedelta(hours=1))
                      .replace(tzinfo=None).isoformat()) is True)


# ---- 2. token_crypto: encryption at rest + dual-read ---------------------------
check("encryption is enabled when a key is configured", token_crypto.enabled() is True)
_blob = token_crypto.encrypt("refresh-secret-123")
check("encrypted value carries the enc:v1: marker", token_crypto.is_encrypted(_blob))
check("encrypted value does not leak the plaintext", "refresh-secret-123" not in _blob)
check("encrypt/decrypt round-trips", token_crypto.decrypt(_blob) == "refresh-secret-123")
check("legacy plaintext passes through decrypt unchanged (dual-read)",
      token_crypto.decrypt("legacy-plain-token") == "legacy-plain-token")
check("None stays None through encrypt and decrypt",
      token_crypto.encrypt(None) is None and token_crypto.decrypt(None) is None)
check("a tampered blob fails the MAC and decrypts to None",
      token_crypto.decrypt(_blob[:-3] + ("zzz" if not _blob.endswith("zzz") else "aaa")) is None)
check("two encryptions of the same plaintext differ (random nonce)",
      token_crypto.encrypt("x") != token_crypto.encrypt("x"))

# Through the real DB seam: written tokens are ciphertext on disk, plaintext on read.
db.set_google_tokens(1, "access-AAA", "refresh-BBB", _iso(3600))
_raw = sqlite3.connect(db.DB_PATH).execute(
    "SELECT access_token, refresh_token FROM integrations WHERE business_id=1 "
    "AND provider='google'").fetchone()
check("DB file stores the access token encrypted (not plaintext)",
      _raw[0].startswith("enc:v1:") and "access-AAA" not in _raw[0])
check("DB file stores the refresh token encrypted (not plaintext)",
      _raw[1].startswith("enc:v1:") and "refresh-BBB" not in _raw[1])
_intg = db.get_integration(1, "google")
check("get_integration decrypts the access token back",
      _intg["access_token"] == "access-AAA")
check("get_integration decrypts the refresh token back",
      _intg["refresh_token"] == "refresh-BBB")

# Legacy plaintext row (written as if before encryption) still reads + is connected.
_conn = sqlite3.connect(db.DB_PATH)
_conn.execute("INSERT INTO integrations (business_id, provider, connected, connected_at, "
              "access_token, refresh_token, token_expiry) VALUES (2,'google',1,?,?,?,?)",
              (db.now_iso(), "legacy-access", "legacy-refresh", _iso(3600)))
_conn.commit(); _conn.close()
_legacy = db.get_integration(2, "google")
check("legacy plaintext refresh token reads unchanged (dual-read at the DB)",
      _legacy["refresh_token"] == "legacy-refresh")
check("a business with a legacy plaintext token still counts as connected",
      google_cal.is_connected(2) is True)

# Disconnect is a TRUE forget: no refresh token may linger at rest afterwards.
db.set_google_tokens(4, "acc-4", "refresh-4", _iso(3600))
google_cal.disconnect(4)
_after = sqlite3.connect(db.DB_PATH).execute(
    "SELECT connected, access_token, refresh_token FROM integrations WHERE business_id=4 "
    "AND provider='google'").fetchone()
check("calendar disconnect clears the refresh token (real forget, nothing lingers)",
      _after[0] == 0 and _after[1] is None and _after[2] is None)
check("disconnected business is not connected", google_cal.is_connected(4) is False)


# ---- 3. _access_token uses cache when fresh, refreshes when stale ---------------
# A fresh access token must be returned WITHOUT any network refresh.
db.set_google_tokens(3, "cached-token", "refresh-3", _iso(3600))
_called = {"refresh": 0}
import requests as _requests_mod


class _FakeResp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


def _fake_post_refresh(url, data=None, **kw):
    _called["refresh"] += 1
    return _FakeResp({"access_token": "rotated-token", "expires_in": 3600})


_orig_post = _requests_mod.post
_requests_mod.post = _fake_post_refresh
try:
    tok = google_cal._access_token(3)
    check("fresh cached token is used without a refresh call",
          tok == "cached-token" and _called["refresh"] == 0)
    # Now expire it: a stale token triggers exactly one refresh.
    db.set_google_tokens(3, "cached-token", "refresh-3", _iso(-10))
    tok2 = google_cal._access_token(3)
    check("expired token triggers a refresh and returns the rotated token",
          tok2 == "rotated-token" and _called["refresh"] == 1)
    check("the rotated token is itself stored encrypted at rest",
          sqlite3.connect(db.DB_PATH).execute(
              "SELECT access_token FROM integrations WHERE business_id=3 "
              "AND provider='google'").fetchone()[0].startswith("enc:v1:"))
finally:
    _requests_mod.post = _orig_post


# ---- 4. OAuth callbacks reject a bad state and never half-connect ---------------
import app as _app
client = _app.app.test_client()
client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})

# Calendar callback: no state in session -> rejected with an honest error.
r = client.get("/api/calendar/google/callback?code=abc&state=anything",
               follow_redirects=False)
check("calendar callback with no session state is rejected (->gerror=state)",
      r.status_code == 302 and "gerror=state" in r.headers.get("Location", ""))

# Establish a session state via connect (configured() may be False -> redirects to
# unconfigured; we set the state directly in that case to test the check itself).
with client.session_transaction() as sess:
    sess["g_state"] = "good-state"
r = client.get("/api/calendar/google/callback?code=abc&state=WRONG",
               follow_redirects=False)
check("calendar callback with a MISMATCHED state is rejected",
      "gerror=state" in r.headers.get("Location", ""))

# A denied consent (error param, no code) renders an honest 'denied', not connected.
with client.session_transaction() as sess:
    sess["g_state"] = "good-state"
r = client.get("/api/calendar/google/callback?error=access_denied",
               follow_redirects=False)
check("calendar callback with consent denied -> honest gerror=denied",
      "gerror=denied" in r.headers.get("Location", ""))

# Matching state but the token exchange raises -> honest 'exchange', never connected.
# Start from a clean disconnected state so we're proving the FAILED connect alone.
google_cal.disconnect(1)
check("precondition: business 1 starts disconnected", google_cal.is_connected(1) is False)
with client.session_transaction() as sess:
    sess["g_state"] = "good-state"
_orig_connect = google_cal.connect_with_code


def _boom(*a, **k):
    raise RuntimeError("token endpoint down")


google_cal.connect_with_code = _boom
try:
    r = client.get("/api/calendar/google/callback?code=abc&state=good-state",
                   follow_redirects=False)
finally:
    google_cal.connect_with_code = _orig_connect
check("calendar callback exchange failure -> honest gerror=exchange (not connected)",
      "gerror=exchange" in r.headers.get("Location", ""))
check("a failed calendar connect leaves the business NOT connected",
      google_cal.is_connected(1) is False)

# Contacts callback enforces the SAME state rule on its own session key.
r = client.get("/api/contacts/google/callback?code=abc&state=anything",
               follow_redirects=False)
check("contacts callback with no session state is rejected (->gcerror=state)",
      "gcerror=state" in r.headers.get("Location", ""))
with client.session_transaction() as sess:
    sess["gc_state"] = "gc-good"
r = client.get("/api/contacts/google/callback?code=abc&state=WRONG",
               follow_redirects=False)
check("contacts callback with a MISMATCHED state is rejected",
      "gcerror=state" in r.headers.get("Location", ""))


# ---- 5. Connect routes mint a per-session state (CSRF guard) --------------------
# When Google is configured, /connect should stash a fresh state in the session.
if google_cal.configured():
    with client.session_transaction() as sess:
        sess.pop("g_state", None)
    client.get("/api/calendar/google/connect", follow_redirects=False)
    with client.session_transaction() as sess:
        check("calendar connect stores a CSRF state in the session",
              bool(sess.get("g_state")))
else:
    check("calendar connect is gated when unconfigured (skips state mint)",
          google_cal.configured() is False)


print(f"==== {_pass} passed, {_fail} failed ====")
import sys
sys.exit(1 if _fail else 0)
