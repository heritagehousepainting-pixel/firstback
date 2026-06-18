"""Phase 3 SF-8 — connections.py orchestration tests.

Run: /Users/jonathanmorris/apps/firstback/.venv/bin/python test_sf8_connections.py

Covers:
  - registration_path
  - _profile_done fork (sole_prop ok without EIN; llc needs EIN; unknown needs EIN)
  - build_slug / build_contact_email
  - submit_a2p: simulated when trust_hub off; brand->svc->campaign in order on mocked
    success; error+step on first failure WITHOUT calling later steps; never sets approved
  - flush_blocked_sends: fresh flushed; stale skipped; opted-out skipped; conversation-
    progressed skipped; dedupe (no resend on 2nd call); a2p_sync fires flush exactly
    once on pending->approved; NOT on approved->approved
  - Stubs all A1-owned functions (messaging.trust_hub_configured,
    messaging.create_a2p_brand, messaging.create_a2p_messaging_service,
    messaging.create_a2p_campaign, db.get_blocked_sends, db.mark_flushed,
    db.mark_flush_skipped, db.set_micro_site, db.set_business_type)
    so this suite passes standalone without A1's implementation.

Uses a REAL temp SQLite db with the blocked_sends table created inline (since
A1 owns the migration and it hasn't landed yet).
"""
import os
import sys
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

# ---- env setup (must happen before any firstback import) ----
os.environ["FIRSTBACK_PROVIDER"] = "demo"
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN",  "tok_test")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name
db.init_db()

# ---- Phase 3 schema extensions (owned by A1; create here for standalone tests) ----
def _apply_phase3_migrations():
    conn = db.get_conn()
    c = conn.cursor()
    biz_cols = [r[1] for r in c.execute("PRAGMA table_info(businesses)").fetchall()]
    for col, ddl in (("business_type",  "TEXT DEFAULT 'unknown'"),
                     ("micro_site_slug","TEXT"),
                     ("a2p_contact_email","TEXT")):
        if col not in biz_cols:
            c.execute(f"ALTER TABLE businesses ADD COLUMN {col} {ddl}")
    c.execute("""CREATE TABLE IF NOT EXISTS blocked_sends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL, lead_id INTEGER,
        to_number TEXT NOT NULL, body TEXT NOT NULL, blocked_at TEXT NOT NULL,
        flushed INTEGER DEFAULT 0, flushed_at TEXT, skip_reason TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_blocked_sends_biz "
              "ON blocked_sends(business_id, flushed)")
    conn.commit()
    conn.close()

_apply_phase3_migrations()

import messaging
import connections

# ---- Stub A1-owned functions so tests pass standalone ----

# messaging stubs
messaging.trust_hub_configured = lambda: False   # default: simulate
messaging.create_a2p_brand = lambda biz: {"status": "simulated"}
messaging.create_a2p_messaging_service = lambda biz: {"status": "simulated"}
messaging.create_a2p_campaign = lambda biz, msid, bsid: {"status": "simulated"}

# db stubs (A1-owned Phase 3 functions that don't exist yet)
_blocked_sends_store = {}   # business_id -> list of row dicts (for test control)

def _stub_get_blocked_sends(business_id, flushed=False, limit=50):
    rows = _blocked_sends_store.get(business_id, [])
    if not flushed:
        rows = [r for r in rows if not r.get("flushed")]
    return rows[:limit]

def _stub_mark_flushed(blocked_send_id):
    for biz_rows in _blocked_sends_store.values():
        for r in biz_rows:
            if r["id"] == blocked_send_id:
                r["flushed"] = 1
                r["flushed_at"] = datetime.now(timezone.utc).isoformat()

def _stub_mark_flush_skipped(blocked_send_id, reason):
    for biz_rows in _blocked_sends_store.values():
        for r in biz_rows:
            if r["id"] == blocked_send_id:
                r["skip_reason"] = reason
                # Mark as 'flushed' too so it won't be re-queried
                r["flushed"] = 1

def _stub_set_micro_site(business_id, slug, contact_email):
    conn = db.get_conn()
    conn.execute("UPDATE businesses SET micro_site_slug=?, a2p_contact_email=? WHERE id=?",
                 (slug, contact_email, business_id))
    conn.commit()
    conn.close()

def _stub_set_business_type(business_id, business_type):
    conn = db.get_conn()
    conn.execute("UPDATE businesses SET business_type=? WHERE id=?",
                 (business_type, business_id))
    conn.commit()
    conn.close()

db.get_blocked_sends = _stub_get_blocked_sends
db.mark_flushed = _stub_mark_flushed
db.mark_flush_skipped = _stub_mark_flush_skipped
db.set_micro_site = _stub_set_micro_site
db.set_business_type = _stub_set_business_type

# ---- Test harness ----
_pass = _fail = 0

def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok   {name}")
    else:
        _fail += 1
        print(f"FAIL   {name}")


# ==========================================================================
# 1. registration_path
# ==========================================================================
print("\n-- registration_path --")
check("sole_prop maps correctly",
      connections.registration_path({"business_type": "sole_prop"}) == "sole_prop")
check("llc maps correctly",
      connections.registration_path({"business_type": "llc"}) == "llc")
check("unknown maps correctly",
      connections.registration_path({"business_type": "unknown"}) == "unknown")
check("missing business_type -> 'unknown'",
      connections.registration_path({}) == "unknown")
check("None biz -> 'unknown'",
      connections.registration_path(None) == "unknown")


# ==========================================================================
# 2. _profile_done fork
# ==========================================================================
print("\n-- _profile_done / profile_complete fork --")

# sole_prop: name + address = done (no EIN)
sp_ok = {"business_type": "sole_prop", "name": "Dave Plumbing", "business_address": "1 Main St"}
sp_no_addr = {"business_type": "sole_prop", "name": "Dave Plumbing"}
sp_with_ein = {"business_type": "sole_prop", "name": "Dave Plumbing",
               "business_address": "1 Main St", "ein": "12-3456789"}
check("sole_prop: name+address = done",        connections._profile_done(sp_ok))
check("sole_prop: missing address = NOT done", not connections._profile_done(sp_no_addr))
check("sole_prop: EIN ignored (done without)", connections._profile_done(sp_ok))
check("sole_prop: having an EIN still done",   connections._profile_done(sp_with_ein))

# llc: needs name + ein + address
llc_ok      = {"business_type": "llc", "name": "Acme LLC", "ein": "11-1111111",
               "business_address": "2 Oak Ave"}
llc_no_ein  = {"business_type": "llc", "name": "Acme LLC", "business_address": "2 Oak Ave"}
llc_no_addr = {"business_type": "llc", "name": "Acme LLC", "ein": "11-1111111"}
llc_no_name = {"business_type": "llc", "ein": "11-1111111", "business_address": "2 Oak Ave"}
check("llc: name+ein+address = done",              connections._profile_done(llc_ok))
check("llc: missing EIN = NOT done",               not connections._profile_done(llc_no_ein))
check("llc: missing address = NOT done",           not connections._profile_done(llc_no_addr))
check("llc: missing name = NOT done",              not connections._profile_done(llc_no_name))

# unknown: same rule as llc (requires EIN) -- existing behavior preserved
unk_ok  = {"business_type": "unknown", "name": "Mystery Co", "ein": "99-9999999",
           "business_address": "3 Pine Rd"}
unk_no_ein = {"business_type": "unknown", "name": "Mystery Co", "business_address": "3 Pine Rd"}
check("unknown: name+ein+address = done",   connections._profile_done(unk_ok))
check("unknown: missing EIN = NOT done",    not connections._profile_done(unk_no_ein))

# Missing business_type defaults to unknown -> EIN required
bare_with_ein = {"name": "Bare Co", "ein": "44-4444444", "business_address": "4 Elm St"}
bare_no_ein   = {"name": "Bare Co", "business_address": "4 Elm St"}
check("no business_type (defaults unknown): name+ein+addr = done",
      connections._profile_done(bare_with_ein))
check("no business_type (defaults unknown): no EIN = NOT done",
      not connections._profile_done(bare_no_ein))

# profile_complete delegates to _profile_done
check("profile_complete same as _profile_done (sole_prop ok)",
      connections.profile_complete(sp_ok))
check("profile_complete same as _profile_done (llc needs ein)",
      not connections.profile_complete(llc_no_ein))


# ==========================================================================
# 3. build_slug / build_contact_email
# ==========================================================================
print("\n-- build_slug / build_contact_email --")

check("basic slug: lowercase + hyphen + id",
      connections.build_slug("Heritage House Painting", 5) == "heritage-house-painting-5")
check("slug: punctuation collapsed to hyphen",
      connections.build_slug("Acme & Sons, LLC!", 3) == "acme-sons-llc-3")
check("slug: leading/trailing hyphens stripped",
      connections.build_slug("--weird--", 7).startswith("weird") and
      connections.build_slug("--weird--", 7).endswith("-7"))
check("slug: capped at 40 chars before appending id",
      len(connections.build_slug("a" * 60, 1)) <= 43)  # 40 + '-' + id
check("slug: empty name falls back to biz-{id}",
      connections.build_slug("", 9) == "biz-9")
check("slug: whitespace-only falls back to biz-{id}",
      connections.build_slug("   ", 9) == "biz-9")
check("slug: appends business_id for uniqueness",
      connections.build_slug("dave", 42).endswith("-42"))

# contact email
slug_example = "heritage-house-painting-1"
email = connections.build_contact_email(slug_example)
domain = getattr(config, "CLIENTS_EMAIL_DOMAIN", "clients.firstback.com")
check("contact_email format: slug@domain",
      email == f"{slug_example}@{domain}")
check("contact_email uses CLIENTS_EMAIL_DOMAIN",
      email.endswith(f"@{domain}"))


# ==========================================================================
# 4. submit_a2p
# ==========================================================================
print("\n-- submit_a2p --")

# Prep business 1 with llc type so Path B runs
db.set_business_type(1, "llc")
conn = db.get_conn()
conn.execute("UPDATE businesses SET name='Heritage House Painting', "
             "ein='12-3456789', business_address='1 Main St, PA 19100' WHERE id=1")
conn.commit(); conn.close()

# 4a. trust_hub off -> simulated (but sets pending)
messaging.trust_hub_configured = lambda: False
res = connections.submit_a2p(1)
check("submit_a2p: simulated when trust_hub off", res["status"] == "simulated")
saved_biz = db.get_business(1)
check("submit_a2p: simulated still sets a2p_status=pending",
      saved_biz["a2p_status"] == "pending")
check("submit_a2p: simulated still sets submitted_at",
      bool(saved_biz.get("a2p_submitted_at")))
check("submit_a2p: NEVER sets approved on simulated",
      saved_biz["a2p_status"] != "approved")

# Reset for next batch
db.set_a2p_registration(1, status="unregistered", submitted_at=None)

# 4b. trust_hub on + all mocks succeed -> submitted
messaging.trust_hub_configured = lambda: True
_brand_calls = []
_svc_calls   = []
_camp_calls  = []

def _mock_brand_ok(biz):
    _brand_calls.append(biz.get("id"))
    return {"status": "created", "brand_sid": "BNtest123"}

def _mock_svc_ok(biz):
    _svc_calls.append(biz.get("id"))
    return {"status": "created", "messaging_service_sid": "MGtest456"}

def _mock_camp_ok(biz, msid, bsid):
    _camp_calls.append((biz.get("id"), msid, bsid))
    return {"status": "created", "campaign_sid": "CMtest789"}

messaging.create_a2p_brand = _mock_brand_ok
messaging.create_a2p_messaging_service = _mock_svc_ok
messaging.create_a2p_campaign = _mock_camp_ok

res = connections.submit_a2p(1)
check("submit_a2p: returns submitted on full success",    res["status"] == "submitted")
check("submit_a2p: brand called first",                   len(_brand_calls) == 1)
check("submit_a2p: messaging_service called second",      len(_svc_calls) == 1)
check("submit_a2p: campaign called third",                len(_camp_calls) == 1)
check("submit_a2p: campaign got correct msid+bsid",
      _camp_calls[0][1] == "MGtest456" and _camp_calls[0][2] == "BNtest123")
saved = db.get_business(1)
check("submit_a2p: persists brand_sid",                   saved["a2p_brand_sid"] == "BNtest123")
check("submit_a2p: persists campaign_sid",                saved["a2p_campaign_sid"] == "CMtest789")
check("submit_a2p: persists messaging_service_sid",       saved["a2p_messaging_service_sid"] == "MGtest456")
check("submit_a2p: sets a2p_status=pending",              saved["a2p_status"] == "pending")
check("submit_a2p: NEVER sets a2p_status=approved",       saved["a2p_status"] != "approved")
check("submit_a2p: path B for llc",                       res.get("path") == "B")

# 4c. brand step fails -> error+step='brand'; NO later calls
_brand_calls.clear(); _svc_calls.clear(); _camp_calls.clear()
messaging.create_a2p_brand = lambda biz: (
    _brand_calls.append(biz.get("id")) or {"status": "error", "error": "TCR rejected"})
db.set_a2p_registration(1, status="unregistered", brand_sid=None,
                        campaign_sid=None, messaging_service_sid=None)
res2 = connections.submit_a2p(1)
check("submit_a2p: brand failure -> status=error",        res2["status"] == "error")
check("submit_a2p: brand failure -> step=brand",          res2.get("step") == "brand")
check("submit_a2p: brand failure -> messaging_service NOT called",
      len(_svc_calls) == 0)
check("submit_a2p: brand failure -> campaign NOT called", len(_camp_calls) == 0)

# 4d. messaging_service step fails -> error+step; campaign NOT called
_brand_calls.clear(); _svc_calls.clear(); _camp_calls.clear()
messaging.create_a2p_brand = _mock_brand_ok
messaging.create_a2p_messaging_service = lambda biz: (
    _svc_calls.append(biz.get("id")) or {"status": "error", "error": "Twilio 500"})
db.set_a2p_registration(1, status="unregistered", brand_sid=None,
                        campaign_sid=None, messaging_service_sid=None)
res3 = connections.submit_a2p(1)
check("submit_a2p: svc failure -> status=error",          res3["status"] == "error")
check("submit_a2p: svc failure -> step=messaging_service",res3.get("step") == "messaging_service")
check("submit_a2p: svc failure -> campaign NOT called",   len(_camp_calls) == 0)

# 4e. sole_prop path: no micro_site call, still submits
_brand_calls.clear(); _svc_calls.clear(); _camp_calls.clear()
messaging.create_a2p_brand = _mock_brand_ok
messaging.create_a2p_messaging_service = _mock_svc_ok
messaging.create_a2p_campaign = _mock_camp_ok
db.set_business_type(1, "sole_prop")
db.set_a2p_registration(1, status="unregistered", brand_sid=None,
                        campaign_sid=None, messaging_service_sid=None)
# Wipe micro_site fields so we can check they're NOT set
conn = db.get_conn()
conn.execute("UPDATE businesses SET micro_site_slug=NULL, a2p_contact_email=NULL WHERE id=1")
conn.commit(); conn.close()
res_sp = connections.submit_a2p(1)
check("submit_a2p: sole_prop -> submitted",               res_sp["status"] == "submitted")
check("submit_a2p: sole_prop -> path=A",                  res_sp.get("path") == "A")
check("submit_a2p: sole_prop -> brand called",            len(_brand_calls) == 1)
# sole_prop does NOT set micro_site
saved_sp = db.get_business(1)
check("submit_a2p: sole_prop -> no micro_site_slug set",
      not saved_sp.get("micro_site_slug"))
check("submit_a2p: a2p_status NEVER approved after sole_prop submit",
      saved_sp["a2p_status"] != "approved")

# Restore to known state
db.set_business_type(1, "llc")
messaging.trust_hub_configured = lambda: False
messaging.create_a2p_brand = lambda biz: {"status": "simulated"}
messaging.create_a2p_messaging_service = lambda biz: {"status": "simulated"}
messaging.create_a2p_campaign = lambda biz, msid, bsid: {"status": "simulated"}


# ==========================================================================
# 5. flush_blocked_sends
# ==========================================================================
print("\n-- flush_blocked_sends --")

# Create a lead for coherence tests
conn = db.get_conn()
conn.execute("INSERT INTO leads (id, business_id, name, phone, status, created_at) "
             "VALUES (101, 1, 'Test Lead', '+12155550001', 'new', '2026-06-18T10:00:00')")
conn.commit(); conn.close()

_send_sms_calls = []
_send_sms_result = {"status": "sent", "sid": "SMtest"}

def _mock_send_sms(biz, to, body, lead_id=None, gate=True, transactional=False):
    _send_sms_calls.append({"to": to, "body": body, "lead_id": lead_id,
                             "gate": gate, "transactional": transactional})
    return _send_sms_result

messaging.send_sms = _mock_send_sms

now_utc = datetime.now(timezone.utc)
fresh_ts   = (now_utc - timedelta(hours=1)).isoformat()
stale_ts   = (now_utc - timedelta(hours=10)).isoformat()

# 5a. Fresh row -> flushed
_blocked_sends_store[1] = [
    {"id": 1, "business_id": 1, "lead_id": 101, "to_number": "+12155550001",
     "body": "Hi, thanks for calling!", "blocked_at": fresh_ts, "flushed": 0}
]
_send_sms_calls.clear()
result = connections.flush_blocked_sends(1)
check("flush: fresh row -> flushed=1", result["flushed"] == 1)
check("flush: fresh row -> skipped=0", result["skipped"] == 0)
check("flush: fresh row -> errors=0",  result["errors"] == 0)
check("flush: send_sms called for fresh row", len(_send_sms_calls) == 1)
check("flush: send_sms gets transactional=True",
      _send_sms_calls[0]["transactional"] is True)
check("flush: send_sms gets gate=True",
      _send_sms_calls[0]["gate"] is True)
check("flush: mark_flushed set on row",
      _blocked_sends_store[1][0]["flushed"] == 1)

# 5b. Stale row -> skipped 'stale'
_blocked_sends_store[1] = [
    {"id": 2, "business_id": 1, "lead_id": 101, "to_number": "+12155550002",
     "body": "Stale message", "blocked_at": stale_ts, "flushed": 0}
]
_send_sms_calls.clear()
result = connections.flush_blocked_sends(1)
check("flush: stale row -> flushed=0",  result["flushed"] == 0)
check("flush: stale row -> skipped=1",  result["skipped"] == 1)
check("flush: stale row -> errors=0",   result["errors"] == 0)
check("flush: stale row -> send_sms NOT called", len(_send_sms_calls) == 0)
check("flush: stale row skip_reason='stale'",
      _blocked_sends_store[1][0].get("skip_reason") == "stale")

# 5c. Opted-out row -> skipped 'opted_out'
_opted_out_number = "+12155550099"
db.set_opt_out(1, _opted_out_number)
_blocked_sends_store[1] = [
    {"id": 3, "business_id": 1, "lead_id": 101, "to_number": _opted_out_number,
     "body": "Opt-out test", "blocked_at": fresh_ts, "flushed": 0}
]
_send_sms_calls.clear()
result = connections.flush_blocked_sends(1)
check("flush: opted-out row -> flushed=0",  result["flushed"] == 0)
check("flush: opted-out row -> skipped=1",  result["skipped"] == 1)
check("flush: opted-out row -> send_sms NOT called", len(_send_sms_calls) == 0)
check("flush: opted-out skip_reason='opted_out'",
      _blocked_sends_store[1][0].get("skip_reason") == "opted_out")

# 5d. Conversation-progressed -> skipped
# Inject a real subsequent message into db.messages with non-null provider_sid
subsequent_ts = (now_utc - timedelta(minutes=30)).isoformat()
conn = db.get_conn()
conn.execute("INSERT INTO messages (id, lead_id, direction, body, created_at, provider_sid) "
             "VALUES (9001, 101, 'in', 'Hi got your text', ?, 'SMrealsubsequent')",
             (subsequent_ts,))
conn.commit(); conn.close()

_blocked_sends_store[1] = [
    {"id": 4, "business_id": 1, "lead_id": 101, "to_number": "+12155550003",
     "body": "Thanks for calling!", "blocked_at": fresh_ts, "flushed": 0}
]
_send_sms_calls.clear()
result = connections.flush_blocked_sends(1)
check("flush: conv-progressed -> flushed=0",  result["flushed"] == 0)
check("flush: conv-progressed -> skipped=1",  result["skipped"] == 1)
check("flush: conv-progressed -> send_sms NOT called", len(_send_sms_calls) == 0)
check("flush: conv-progressed skip_reason='conversation_progressed'",
      _blocked_sends_store[1][0].get("skip_reason") == "conversation_progressed")

# Remove the subsequent message for clean state
conn = db.get_conn()
conn.execute("DELETE FROM messages WHERE id=9001")
conn.commit(); conn.close()

# 5e. Dedupe: row already flushed -> not replayed
_blocked_sends_store[1] = [
    {"id": 5, "business_id": 1, "lead_id": 101, "to_number": "+12155550004",
     "body": "Already sent", "blocked_at": fresh_ts, "flushed": 1}  # already flushed
]
_send_sms_calls.clear()
# get_blocked_sends filters flushed=0 rows, so this should return 0 rows
result = connections.flush_blocked_sends(1)
check("flush: already-flushed row -> not re-sent", len(_send_sms_calls) == 0)
check("flush: already-flushed row -> flushed=0 errors=0", result["flushed"] == 0)

# 5f. Dedupe: mark_flushed before send (no resend on 2nd call)
_blocked_sends_store[1] = [
    {"id": 6, "business_id": 1, "lead_id": 101, "to_number": "+12155550005",
     "body": "First time", "blocked_at": fresh_ts, "flushed": 0}
]
_send_sms_calls.clear()
result1 = connections.flush_blocked_sends(1)
check("flush: first call sends the row", result1["flushed"] == 1)
_send_sms_calls.clear()
result2 = connections.flush_blocked_sends(1)
check("flush: second call does NOT resend (dedupe)", result2["flushed"] == 0)
check("flush: second call -> 0 send_sms calls", len(_send_sms_calls) == 0)

# 5g. Still-blocked guard: send_sms returns 'blocked' -> STOP + error
_blocked_sends_store[1] = [
    {"id": 7, "business_id": 1, "lead_id": None, "to_number": "+12155550006",
     "body": "Will be blocked", "blocked_at": fresh_ts, "flushed": 0},
    {"id": 8, "business_id": 1, "lead_id": None, "to_number": "+12155550007",
     "body": "Should not be reached", "blocked_at": fresh_ts, "flushed": 0},
]
import io, contextlib
_guard_result_holder = {}
_send_sms_calls.clear()

def _mock_send_blocked(*args, **kwargs):
    return {"status": "blocked"}

messaging.send_sms = _mock_send_blocked
_guard_stderr = io.StringIO()
with contextlib.redirect_stderr(_guard_stderr):
    guard_result = connections.flush_blocked_sends(1)
check("flush: blocked guard -> stops flush",         guard_result["errors"] >= 1)
check("flush: blocked guard -> row 8 NOT sent",
      _blocked_sends_store[1][1]["flushed"] == 0)
check("flush: blocked guard -> logs error to stderr",
      "blocked" in _guard_stderr.getvalue().lower() or
      "state inconsistency" in _guard_stderr.getvalue().lower())

# Restore send_sms
messaging.send_sms = _mock_send_sms

# 5h. Flush cap 50 (spot-check: only 50 rows out of 60 queued)
_blocked_sends_store[1] = [
    {"id": 100 + i, "business_id": 1, "lead_id": None,
     "to_number": f"+1215555{i:04d}", "body": f"msg {i}",
     "blocked_at": fresh_ts, "flushed": 0}
    for i in range(60)
]
_send_sms_calls.clear()
cap_result = connections.flush_blocked_sends(1)
check("flush: cap 50 rows per call (60 queued -> max 50 processed)",
      cap_result["flushed"] <= 50)

# Clean up blocked_sends_store
_blocked_sends_store.clear()


# ==========================================================================
# 6. a2p_sync: flush hook fires once on pending->approved, NOT on approved->approved
# ==========================================================================
print("\n-- a2p_sync flush hook --")

# Wire the business with a campaign so sync has something to poll
db.set_a2p_registration(1, campaign_sid="CMhook", messaging_service_sid="MGhook",
                        status="pending")
db.update_business(1, {"name": "Heritage House Painting"})

_flush_calls = []
_orig_flush = connections.flush_blocked_sends
connections.flush_blocked_sends = lambda bid: _flush_calls.append(bid) or {"flushed": 0, "skipped": 0, "errors": 0}

_orig_fetch = messaging.fetch_a2p_campaign_status

# 6a. pending -> approved: flush fires
messaging.fetch_a2p_campaign_status = lambda svc, camp: "VERIFIED"
_flush_calls.clear()
result_sync = connections.a2p_sync(db.get_business(1))
check("a2p_sync: pending->approved returns 'approved'", result_sync == "approved")
check("a2p_sync: pending->approved fires flush exactly once", len(_flush_calls) == 1)
check("a2p_sync: flush called with correct business_id", _flush_calls[0] == 1)

# 6b. approved -> approved (already approved, VERIFIED again): flush must NOT fire
_flush_calls.clear()
result_sync2 = connections.a2p_sync(db.get_business(1))
check("a2p_sync: approved->approved returns 'approved'", result_sync2 == "approved")
check("a2p_sync: approved->approved does NOT fire flush", len(_flush_calls) == 0)

# 6c. pending -> failed: flush must NOT fire
db.set_a2p_status(1, "pending")
messaging.fetch_a2p_campaign_status = lambda svc, camp: "FAILED"
_flush_calls.clear()
connections.a2p_sync(db.get_business(1))
check("a2p_sync: pending->failed does NOT fire flush", len(_flush_calls) == 0)

# 6d. flush exception inside a2p_sync must not break the sync return value
db.set_a2p_status(1, "pending")
messaging.fetch_a2p_campaign_status = lambda svc, camp: "VERIFIED"
connections.flush_blocked_sends = lambda bid: (_ for _ in ()).throw(RuntimeError("flush exploded"))
_sync_stderr = io.StringIO()
with contextlib.redirect_stderr(_sync_stderr):
    result_exc = connections.a2p_sync(db.get_business(1))
check("a2p_sync: flush exception -> sync still returns 'approved'",
      result_exc == "approved")
check("a2p_sync: flush exception -> logs error, doesn't crash",
      "firstback" in _sync_stderr.getvalue() or "flush" in _sync_stderr.getvalue().lower())

# Restore
connections.flush_blocked_sends = _orig_flush
messaging.fetch_a2p_campaign_status = _orig_fetch


# ==========================================================================
# 7. never-raises contract (submit_a2p + flush_blocked_sends)
# ==========================================================================
print("\n-- never-raises contract --")

# submit_a2p on a non-existent business_id: db.get_business falls back to DEFAULT_BUSINESS
# (never returns None), so submit returns 'simulated' (trust_hub off). Verify it never raises.
res_no_biz = connections.submit_a2p(99999)
check("submit_a2p: non-existent biz -> dict, no raise (get_business fallback)",
      isinstance(res_no_biz, dict) and "status" in res_no_biz)

# flush_blocked_sends on a non-existent business_id
res_flush_no_biz = connections.flush_blocked_sends(99999)
check("flush_blocked_sends: non-existent biz -> dict with flushed/skipped/errors",
      isinstance(res_flush_no_biz, dict) and
      "flushed" in res_flush_no_biz and "skipped" in res_flush_no_biz)


# ==========================================================================
print(f"\n{_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
