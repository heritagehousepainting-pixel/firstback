"""Batch C -- Mobile + dashboard UX (plan 04). Run: python3 test_batch_c.py

Proves the buildable-without-owner mobile/dashboard surfaces render correctly:
  1. /pipeline leads card: desktop table (.leads-table-wrap) AND the mobile card list
     (.lead-cards / .lead-card) render the same leads, with tel: links and the urgent tint.
  2. Phone numbers across the leads / appointments / screened tables are tel: links.
  3. /pipeline?lead_id=X (briefing deep-link target) still renders 200.
  4. /dashboard command center always shows an orientation block (briefing items OR the
     'all clear' last-lead line) and ships the visible daily-cap nudge handler.
  5. The shared static assets carry the new error-card + card-list + urgency CSS/JS.
  6. _time_ago() formats a best-effort 'ago' string and never raises.

Throwaway temp DB, demo brain -- mirrors the other *_ui tests.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["FIRSTBACK_PROVIDER"] = "demo"
import config
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _TMP.close()
config.DB_PATH = _TMP.name

import db
db.DB_PATH = _TMP.name

import messaging
messaging.TWILIO_ACCOUNT_SID = ""   # configured() False -> simulate, never a real send

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


# --- seed: two leads, one urgent ---
_urgent_id = db.create_lead(1, "Dave Carter", "+15551234567")
db.mark_lead_urgent(_urgent_id, 1)
db.create_lead(1, "Jane Smith", "+15559876543")

client.post("/login", data={"email": config.SEED_OWNER_EMAIL,
                            "password": config.SEED_OWNER_PASSWORD})

# --- 1) /pipeline leads: table + parallel card list ---
r = client.get("/pipeline")
h = r.get_data(as_text=True)
check("/pipeline renders 200", r.status_code == 200)
check("desktop table is wrapped for mobile hide (.leads-table-wrap)", 'class="leads-table-wrap"' in h)
check("mobile card list container present (.lead-cards)", 'class="lead-cards"' in h)
check("lead cards reuse .dt-row[data-id] so JS row-open works", 'class="lead-card dt-row' in h)
check("urgent lead gets the is-urgent class", "dt-row is-urgent" in h)
check("urgent lead card gets the urgency dot", "lead-card-dot" in h)
# a11y fix: the interactive card is a button (matches the table rows + the aria-pressed JS),
# not a listitem inside a list.
check("lead card uses role=button (a11y audit fix)", 'role="button"' in h and "lead-card dt-row" in h)
check("lead-cards container is not a role=list", 'class="lead-cards" aria-label="Leads"' in h)

# --- 2) tel: links on the leads table + cards ---
# 2 leads -> 2 table rows + 2 cards = 4 tel: links on the leads card alone.
check("phone numbers are tel: links", h.count('class="tel-link"') >= 4 or h.count("tel-link") >= 4)
check("the urgent lead's number is a real dialer href", 'href="tel:+15551234567"' in h)

# --- 3) deep-link target renders ---
dl = client.get(f"/pipeline?lead_id={_urgent_id}")
check("/pipeline?lead_id=X (briefing deep-link) renders 200", dl.status_code == 200)

# --- 4) command center orientation + cap nudge ---
d = client.get("/dashboard")
dh = d.get_data(as_text=True)
check("/dashboard renders 200", d.status_code == 200)
check("briefing slot always present", "briefingSlot" in dh)
check("orientation always shown (briefing items OR all-clear)",
      ("briefing-list" in dh) or ("briefing--clear" in dh))
check("all-clear surfaces the most recent lead",
      ("Last lead:" in dh) or ("briefing-list" in dh))
check("visible daily-cap nudge handler shipped", "showCapNudge" in dh)
check("cap nudge no longer the faint resting one-liner",
      "Resting for a moment" not in dh)

# --- 5) shared static assets carry the new CSS/JS ---
appjs = client.get("/static/app.js").get_data(as_text=True)
check("app.js ships the error-card helper", "function addErrorTurn" in appjs)
check("app.js error path uses the error card, not addMeta",
      "addErrorTurn(convo" in appjs)
check("app.js deep-link auto-open present", "lead_id" in appjs)
# a11y/security audit fixes locked in:
check("app.js keydown guards child (keyboard dialing works)", "e.target !== row" in appjs)
check("app.js card-open scroll respects reduced-motion", "prefers-reduced-motion" in appjs)
check("app.js sanitizes lead_id before querySelector", 'replace(/[^0-9]/g' in appjs)
uicss = client.get("/static/ui.css").get_data(as_text=True)
check("ui.css ships the error-card style", ".chat-error-turn" in uicss)
appcss = client.get("/static/app.css").get_data(as_text=True)
check("app.css ships the mobile card-list style", ".lead-card{" in appcss or ".lead-card {" in appcss)
check("app.css ships the urgency tint (:has)", ":has(.stat-sub.bad)" in appcss)
check("app.css ships the tel-link style", ".tel-link" in appcss)
check("app.css keeps nav labels on mobile (no display:none on .nav-item span)",
      ".nav-item span{display:none}" not in appcss)

# --- 6) _time_ago() formatting + safety ---
now = datetime.now(timezone.utc)
check("_time_ago: <1h -> 'just now'", appmod._time_ago((now - timedelta(minutes=10)).isoformat()) == "just now")
check("_time_ago: hours", appmod._time_ago((now - timedelta(hours=3)).isoformat()) == "3h ago")
check("_time_ago: days", appmod._time_ago((now - timedelta(days=2)).isoformat()) == "2d ago")
check("_time_ago: weeks", appmod._time_ago((now - timedelta(days=21)).isoformat()) == "3w ago")
check("_time_ago: None on junk", appmod._time_ago("not-a-date") is None)
check("_time_ago: None on empty", appmod._time_ago(None) is None)

print(f"\n{_pass} passed, {_fail} failed")
raise SystemExit(1 if _fail else 0)
