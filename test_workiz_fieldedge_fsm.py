"""Workiz + FieldEdge FSM providers — gating, connect, parsing regression tests.

Both are token-based (the contractor pastes a per-account API token/key, stored in the
integration refresh_token slot) and gated on an operator enable flag. No network: the REST
layer is monkeypatched; the rest is pure parsing + gating.
"""
import os
import sys
import tempfile

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
db.init_db()
import workiz_fsm as w
import fieldedge_fsm as fe
import fsm_sync

w.WORKIZ_ENABLED = ""
fe.FIELDEDGE_ENABLED = ""

_pass = _fail = 0
def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1; print(f"  ok   {name}")
    else:
        _fail += 1; print(f"FAIL   {name}")


# ---- gating (operator enable flags) ----------------------------------------
check("workiz off by default", w.configured() is False)
check("fieldedge off by default", fe.configured() is False)
w.WORKIZ_ENABLED = "1"; fe.FIELDEDGE_ENABLED = "1"
check("workiz on when enabled", w.configured() is True)
check("fieldedge on when enabled", fe.configured() is True)
check("not connected before a token is stored", w.is_connected(1) is False and fe.is_connected(1) is False)

# ---- connect (paste token; reject empty) -----------------------------------
for mod, label in ((w, "workiz"), (fe, "fieldedge")):
    try:
        mod.connect_token(1, "   ")
        check(f"{label} rejects empty token", False)
    except ValueError:
        check(f"{label} rejects empty token", True)
w.connect_token(1, "wtok"); fe.connect_token(1, "fekey")
check("workiz connected after token", w.is_connected(1) is True)
check("fieldedge connected after token", fe.is_connected(1) is True)

# ---- Workiz parsing --------------------------------------------------------
w._provider._get = lambda biz, res, params=None: (
    iter([{"first_name": "Jane", "last_name": "Doe", "Phone": "2155550101", "email": "j@x.com"},
          {"first_name": "No", "last_name": "Phone"}]) if res == "client"
    else iter([{"JobType": "Repair", "Status": "Submitted", "Phone": "2155550102"}]) if res == "job"
    else iter([]))
check("workiz fetch_clients builds name+phone+email", w.fetch_clients(1) ==
      [{"name": "Jane Doe", "phones": ["2155550101"], "email": "j@x.com"}])
check("workiz skips a client with no phone", all(c["name"] != "No Phone" for c in w.fetch_clients(1)))
check("workiz fetch_jobs maps JobType/Status/phone", w.fetch_jobs(1) ==
      [{"title": "Repair", "status": "Submitted", "client_phone": "2155550102"}])

# ---- FieldEdge parsing -----------------------------------------------------
fe._provider._get = lambda biz, res, params=None: (
    iter([{"firstName": "Bob", "lastName": "Lee", "phone": "2155550109", "email": "b@x.com"}])
    if res == "customers"
    else iter([{"summary": "AC tune-up", "status": "Open", "phone": "2155550110"}]) if res == "jobs"
    else iter([]))
check("fieldedge fetch_clients builds name+phone+email", fe.fetch_clients(1) ==
      [{"name": "Bob Lee", "phones": ["2155550109"], "email": "b@x.com"}])
check("fieldedge fetch_jobs maps summary/status/phone", fe.fetch_jobs(1) ==
      [{"title": "AC tune-up", "status": "Open", "client_phone": "2155550110"}])

# ---- fsm_sync provider selection (priority ServiceTitan>Workiz>FieldEdge>HCP>Jobber) ----
# Only Workiz + FieldEdge connected here (ServiceTitan unconfigured) -> Workiz wins.
active = fsm_sync._get_active_provider(1)
check("fsm_sync picks the higher-priority connected CRM (workiz over fieldedge)",
      active is not None and active.PROVIDER_KEY == "workiz")
check("fsm_sync.configured() true when a token CRM is enabled", fsm_sync.configured() is True)

# ---- disconnect ------------------------------------------------------------
w.disconnect(1); fe.disconnect(1)
check("disconnect clears both", w.is_connected(1) is False and fe.is_connected(1) is False)

print(f"\n{'='*46}")
print(f"Results: {_pass} passed, {_fail} failed")
try:
    os.unlink(_TMP.name)
except OSError:
    pass
sys.exit(1 if _fail else 0)
