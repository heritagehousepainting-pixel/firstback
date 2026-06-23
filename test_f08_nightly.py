"""F08 nightly Google Contacts sync. Run: python3 test_f08_nightly.py

Covers:
  T1 -- inert when not configured (configured()=False)
  T2 -- inert when no business is connected
  T3 -- syncs connected business, skips unconnected
  T4 -- cadence guard: second call same day is a no-op (sync NOT called)
  T5 -- cadence guard: different day triggers sync
  T6 -- exception in one biz's sync is swallowed, others proceed
  T7 -- tick_once() calls google_contacts_sync_all (smoke)
  T8 -- import zero-result 422: contacts found but none have phone numbers

Standalone-script style (print ok/FAIL, sys.exit 0/1). No pytest.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.pop("GOOGLE_CLIENT_ID", None)
os.environ.pop("GOOGLE_CLIENT_SECRET", None)

import config
config.GOOGLE_CLIENT_ID = ""
config.GOOGLE_CLIENT_SECRET = ""

_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

import google_contacts
import reminders
from reminders import google_contacts_sync_all

_pass = _fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


def _today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Seed two businesses ───────────────────────────────────────────────────────
# Business 1 is seeded by init_db() via SEED_OWNER_EMAIL.
# We need a second business; create one via the db layer.
db.get_conn().execute(
    "INSERT OR IGNORE INTO businesses (id, name) VALUES (2, 'Biz Two')"
).connection.commit()

# Reload list to make sure both exist.
_all_biz = db.list_businesses()
_biz_ids = [b["id"] for b in _all_biz]
assert 1 in _biz_ids, f"Biz 1 missing from list_businesses: {_biz_ids}"


# ── T1: inert when not configured ────────────────────────────────────────────
# configured() is False (no GOOGLE_CLIENT_ID set).
result = google_contacts_sync_all()
check("T1: returns dict when not configured", isinstance(result, dict))
check("T1: businesses_checked=0 when not configured", result.get("businesses_checked") == 0)
check("T1: businesses_synced=0 when not configured", result.get("businesses_synced") == 0)
check("T1: suggestions_created=0 when not configured", result.get("suggestions_created") == 0)
check("T1: no DB write (cadence key absent)", db.get_meta("contacts_sync_date:1") is None)


# ── T2: inert when no business is connected ───────────────────────────────────
_orig_configured = google_contacts.configured
_orig_is_connected = google_contacts.is_connected
_orig_sync = google_contacts.sync

google_contacts.configured = lambda: True
google_contacts.is_connected = lambda biz_id: False   # nobody connected

result = google_contacts_sync_all()
check("T2: businesses_checked > 0", result.get("businesses_checked", 0) > 0)
check("T2: businesses_synced=0 when none connected", result.get("businesses_synced") == 0)
check("T2: suggestions_created=0 when none connected", result.get("suggestions_created") == 0)

google_contacts.is_connected = _orig_is_connected   # restore


# ── T3: syncs connected biz, skips unconnected ───────────────────────────────
_sync_called_for = []

def _mock_sync(biz_id):
    _sync_called_for.append(biz_id)
    return {"contacts": 5, "suggested": 3, "skipped": 1, "unclassified": 1,
            "customers": 1, "vendors": 2}

# Clear any cadence keys from previous runs.
_conn = db.get_conn()
_conn.execute("DELETE FROM meta WHERE key LIKE 'contacts_sync_date:%'")
_conn.commit()
_conn.close()

google_contacts.is_connected = lambda biz_id: biz_id == 1   # only biz 1
google_contacts.sync = _mock_sync

result = google_contacts_sync_all()
check("T3: synced=1", result.get("businesses_synced") == 1)
check("T3: created=3", result.get("suggestions_created") == 3)
check("T3: sync called only for biz 1", _sync_called_for == [1])
check("T3: cadence key written for biz 1", db.get_meta("contacts_sync_date:1") == _today_utc())
check("T3: cadence key absent for biz 2", db.get_meta("contacts_sync_date:2") is None)


# ── T4: cadence guard -- second call same day is a no-op ─────────────────────
_sync_called_t4 = []

def _must_not_be_called(biz_id):
    _sync_called_t4.append(biz_id)
    raise AssertionError(f"sync must NOT be called (cadence guard) for biz {biz_id}")

google_contacts.sync = _must_not_be_called
# cadence key for biz 1 was just set by T3 to today_utc.

result = google_contacts_sync_all()
check("T4: no exception raised (cadence guard works)", True)  # would have propagated
check("T4: synced=0 (already ran today)", result.get("businesses_synced") == 0)
check("T4: sync not called at all", _sync_called_t4 == [])


# ── T5: cadence guard -- old date triggers sync ───────────────────────────────
db.set_meta("contacts_sync_date:1", "2000-01-01")
_sync_called_t5 = []

def _mock_sync_t5(biz_id):
    _sync_called_t5.append(biz_id)
    return {"contacts": 2, "suggested": 2, "skipped": 0, "unclassified": 0,
            "customers": 1, "vendors": 1}

google_contacts.sync = _mock_sync_t5

result = google_contacts_sync_all()
check("T5: synced=1 (stale date triggers re-sync)", result.get("businesses_synced") == 1)
check("T5: sync called for biz 1", 1 in _sync_called_t5)
check("T5: cadence key updated to today", db.get_meta("contacts_sync_date:1") == _today_utc())


# ── T6: exception in one biz swallowed, others proceed ───────────────────────
# Reset cadence keys so both biz 1 and 2 are eligible.
_conn = db.get_conn()
_conn.execute("DELETE FROM meta WHERE key LIKE 'contacts_sync_date:%'")
_conn.commit()
_conn.close()

google_contacts.is_connected = lambda biz_id: True   # both connected
_sync_called_t6 = []

def _mock_sync_t6(biz_id):
    _sync_called_t6.append(biz_id)
    if biz_id == 1:
        raise RuntimeError("Simulated API error for biz 1")
    return {"contacts": 1, "suggested": 1, "skipped": 0, "unclassified": 0,
            "customers": 1, "vendors": 0}

google_contacts.sync = _mock_sync_t6

try:
    result = google_contacts_sync_all()
    check("T6: no exception propagated", True)
except Exception as exc:
    check("T6: no exception propagated", False)
    result = {"businesses_synced": 0}

# Both businesses should have been attempted; biz 2 succeeded.
check("T6: synced=1 (biz 2 succeeded)", result.get("businesses_synced") == 1)
check("T6: both biz attempted", set(_sync_called_t6) == {1, 2})


# ── T7: tick_once calls google_contacts_sync_all ─────────────────────────────
_called = []
_orig_sync_all = reminders.google_contacts_sync_all

def _fake_sync_all(now=None):
    _called.append(True)
    return {"businesses_checked": 0, "businesses_synced": 0, "suggestions_created": 0}

reminders.google_contacts_sync_all = _fake_sync_all

result_tick = reminders.tick_once()
check("T7: google_contacts_sync_all was called by tick_once", bool(_called))
check("T7: tick_once returns a dict", isinstance(result_tick, dict))
check("T7: tick_once return dict has contacts_synced key", "contacts_synced" in result_tick)

reminders.google_contacts_sync_all = _orig_sync_all


# ── T8: import zero-result 422 (contacts found, none have phones) ────────────
import app as _app
from io import BytesIO

_client = _app.app.test_client()
_client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                             "password": config.SEED_OWNER_PASSWORD})
with _client.session_transaction() as _sess:
    _sess["csrf_token"] = "test_csrf"
_client.environ_base["HTTP_X_CSRF_TOKEN"] = "test_csrf"

# A vCard with contacts but NO phone numbers at all.
VCARD_NO_PHONES = b"""BEGIN:VCARD
VERSION:3.0
FN:Alice NoPhone
EMAIL:alice@example.com
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Bob NoPhone
EMAIL:bob@example.com
END:VCARD
"""

r8 = _client.post(
    "/api/contacts/import",
    data={"file": (BytesIO(VCARD_NO_PHONES), "contacts.vcf")},
    content_type="multipart/form-data",
)
check("T8: 422 when contacts found but none have phones", r8.status_code == 422)
j8 = r8.get_json() or {}
check("T8: error message is human-readable (non-empty string)",
      isinstance(j8.get("error"), str) and len(j8["error"]) > 0)

# Restore originals.
google_contacts.configured = _orig_configured
google_contacts.is_connected = _orig_is_connected
google_contacts.sync = _orig_sync

try:
    os.unlink(_TMP.name)
except OSError:
    pass

print(f"\n{_pass} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
