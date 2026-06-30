"""Section scaffolding: remaining pages (/dashboard, /training, /callers, /customers).
Run: python3 test_sections.py
"""
import os, sys, tempfile
os.environ.setdefault("FIRSTBACK_PROVIDER", "demo")

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
import app as _app

client = _app.app.test_client()
db.init_db()
client.post("/login", data={"email": config.SEED_OWNER_EMAIL, "password": config.SEED_OWNER_PASSWORD})

_pass = _fail = 0
def check(name, cond):
    global _pass, _fail
    if cond: _pass += 1; print(f"  ok   {name}")
    else: _fail += 1; print(f"FAIL   {name}")

# /dashboard — single section, existing id='command' preserved for assistant.js
dhtml = client.get("/dashboard").data
check("/dashboard 200", client.get("/dashboard").status_code == 200)
check("/dashboard: class=app-section present",  b'app-section' in dhtml)
check("/dashboard: id=command present",         b'id="command"' in dhtml)
check("/dashboard: command-shell class still present", b'command-shell' in dhtml)
check("/dashboard: no sec-nav (single section)", b'sec-nav' not in dhtml)

# /training — 3 sections: mem-review, mem-teachings, mem-history
thtml = client.get("/training").data
check("/training 200", client.get("/training").status_code == 200)
for sid in ('mem-review', 'mem-teachings', 'mem-history'):
    check(f"/training: id={sid!r} present", f'id="{sid}"'.encode() in thtml)
    check(f"/training: data-label present for {sid}",
          b'data-label=' in thtml)  # at least one
check("/training: sec-nav rendered (3 sections)", b'sec-nav' in thtml)

# /callers — 3 sections: cal-import, cal-review, cal-screened
chtml = client.get("/callers").data
check("/callers 200", client.get("/callers").status_code == 200)
for sid in ('cal-import', 'cal-review', 'cal-screened'):
    check(f"/callers: id={sid!r} present", f'id="{sid}"'.encode() in chtml)
check("/callers: outer div id=callers preserved", b'id="callers"' in chtml)
check("/callers: Import your contacts text present", b'Import your contacts' in chtml)
check("/callers: Screened numbers text present", b'Screened numbers' in chtml)

# /customers — single section: cust-book
khtml = client.get("/customers").data
check("/customers 200", client.get("/customers").status_code == 200)
check("/customers: id=cust-book present", b'id="cust-book"' in khtml)
check("/customers: no sec-nav (single section)", b'sec-nav' not in khtml)

_fail and sys.exit(1)
print(f"\n{_pass} passed, {_fail} failed")
os.unlink(_TMP.name)
