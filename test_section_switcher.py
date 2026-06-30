"""Section-switcher: registry, sub-nav rendering, conditional filtering.
Run: python3 test_section_switcher.py
"""
import os, sys, tempfile
os.environ.setdefault("FIRSTBACK_PROVIDER", "demo")
# Twilio vars needed for messaging.configured() / connections.is_live() in test 11.
# Must be set before config.py is imported (values are read at module load time).
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest_sec")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "tok_sec")
os.environ.setdefault("TWILIO_FROM_NUMBER", "+12677562454")

import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name
import db
db.DB_PATH = _TMP.name
import app as _app

client = _app.app.test_client()
db.init_db()
client.post("/login", data={"email": config.SEED_OWNER_EMAIL, "password": config.SEED_OWNER_PASSWORD})
with client.session_transaction() as _s:
    _s["csrf_token"] = "test_csrf"

_pass = _fail = 0
def check(name, cond):
    global _pass, _fail
    if cond: _pass += 1; print(f"  ok   {name}")
    else: _fail += 1; print(f"FAIL   {name}")

from app import PAGE_SECTIONS

# 1 — registry shape: /settings
s = PAGE_SECTIONS.get('/settings', [])
ids = [e['id'] for e in s]
check("PAGE_SECTIONS['/settings'] has >=14 entries", len(s) >= 14)
check("PAGE_SECTIONS['/settings'] contains set-voice",    'set-voice'    in ids)
check("PAGE_SECTIONS['/settings'] contains set-billing",  'set-billing'  in ids)
check("PAGE_SECTIONS['/settings'] contains set-password", 'set-password' in ids)
check("PAGE_SECTIONS['/settings'] contains set-setup",    'set-setup'    in ids)

# 2 — registry shape: /pipeline (must be 4 sections)
p = PAGE_SECTIONS.get('/pipeline', [])
check("PAGE_SECTIONS['/pipeline'] has exactly 4 entries", len(p) == 4)
check("PAGE_SECTIONS['/pipeline'][0].id == dash-overview", p[0]['id'] == 'dash-overview')
check("PAGE_SECTIONS['/pipeline'] contains dash-activity", any(e['id'] == 'dash-activity' for e in p))
check("PAGE_SECTIONS['/pipeline'] contains dash-roi",      any(e['id'] == 'dash-roi'      for e in p))

# 3 — registry shape: /training uses mem-teachings NOT mem-learned
t = PAGE_SECTIONS.get('/training', [])
t_ids = [e['id'] for e in t]
check("PAGE_SECTIONS['/training'] has 3 entries", len(t) == 3)
check("PAGE_SECTIONS['/training'] uses mem-teachings",  'mem-teachings' in t_ids)
check("PAGE_SECTIONS['/training'] does NOT use mem-learned", 'mem-learned' not in t_ids)

# 4 — registry shape: /dashboard uses 'command' id
d = PAGE_SECTIONS.get('/dashboard', [])
check("PAGE_SECTIONS['/dashboard'] has 1 entry with id='command'",
      len(d) == 1 and d[0]['id'] == 'command')

# 5 — GET /settings: set-setup absent from HTML when not golive (default new account)
html = client.get("/settings").data
check("settings: set-setup id absent from HTML when not golive",
      b'id="set-setup"' not in html)
# 6 — GET /settings: sub-nav renders for sectioned page
check("settings: sec-nav rendered (sub-nav class present)",         b'sec-nav'      in html)
check("settings: sub-nav link to set-voice present",                b'#set-voice'   in html)
check("settings: sub-nav link to set-billing present",              b'#set-billing' in html)
# 7 — section ids present in settings HTML (template + registry in sync)
for sec_id in ['set-profile','set-voice','set-calendar','set-crm','set-screening',
               'set-scheduling','set-alerts','set-reminders','set-widget','set-ai',
               'set-growth','set-billing','set-password']:
    check(f"settings HTML contains id={sec_id!r}",
          f'id="{sec_id}"'.encode() in html)
# 8 — mobile pill-row rendered on /settings
check("settings: section-pill-row rendered (mobile switcher)", b'section-pill-row' in html)

# 9 — GET /pipeline: sub-nav + section ids present
phtml = client.get("/pipeline").data
check("pipeline: sec-nav rendered", b'sec-nav' in phtml)
check("pipeline: #dash-overview link present", b'#dash-overview' in phtml)
check("pipeline: #dash-activity link present", b'#dash-activity' in phtml)
check("pipeline: id=dash-overview in HTML", b'id="dash-overview"' in phtml)
check("pipeline: id=dash-activity in HTML", b'id="dash-activity"' in phtml)
check("pipeline: id=dash-roi in HTML",      b'id="dash-roi"'      in phtml)
check("pipeline: section-pill-row rendered", b'section-pill-row' in phtml)

# 10 — /simulator (non-sectioned page): no sub-nav
shtml = client.get("/simulator").data
check("simulator: no sec-nav on non-sectioned page", b'sec-nav' not in shtml)
check("simulator: no section-pill-row", b'section-pill-row' not in shtml)

# 11 — golive_complete=True: set-setup sub-nav link appears
# Set all conditions required for connections.is_live() to return True
db.set_business_twilio(1, "+12677562454", "PNtest", webhooks_wired=True)
db.set_a2p_status(1, "approved")
db.set_forwarding_confirmed(1, True)
html_live = client.get("/settings").data
check("settings golive: id=set-setup present in HTML", b'id="set-setup"' in html_live)
check("settings golive: #set-setup link in sub-nav",   b'#set-setup'    in html_live)

# 12 — Settings sub-nav grouped into labeled clusters (IA polish)
check("registry: every /settings section has a 'group'",
      all('group' in sec for sec in PAGE_SECTIONS['/settings']))
check("settings: sec-nav-group headers rendered",       b'sec-nav-group' in html)
check("settings: 'Your business' group header present", b'Your business' in html)
check("settings: 'Calls &amp; AI' group header present", b'Calls &amp; AI' in html)
check("settings: 'Account' group header present",       b'Account' in html)
check("pipeline: no group headers (ungrouped page stays flat)",
      b'sec-nav-group' not in phtml)

_fail and sys.exit(1)
print(f"\n{_pass} passed, {_fail} failed")
os.unlink(_TMP.name)
