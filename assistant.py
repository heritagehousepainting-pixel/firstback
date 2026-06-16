"""RingBack's command center -- the conversational control surface (the "Jarvis").

ONE natural-language seam over the product. The signed-in home is a chat: the owner
types "how many leads this week", "show my booked estimates", "save this number as a
customer", "connect my Google calendar", "text my last lead back" -- and this turns it
into a real action against the existing engines (db, messaging, google_cal). No new
source of truth: every tool wraps a function the manual UI already uses.

Same three guarantees as the rest of RingBack:
  1. Provider-agnostic brain (ai._active_provider): Claude or MiniMax when keyed, with a
     deterministic keyword router as the always-works floor.
  2. Tenant-scoped: every tool runs against the signed-in business; nothing crosses tenants.
  3. Honest + gated: read freely; anything that actually texts a customer is returned as a
     pending_action and only sent after an explicit confirm, still through the gated
     messaging.send_sms seam (opt-outs + simulated-vs-live honored).

run(business, message, history) -> {reply, cards, pending_action}
execute(business, tool, args)   -> {reply, cards}
"""
import json
import re

import db
import ai
import messaging

# Account links the chat can offer, with the words an owner is likely to say.
_CONNECT = {
    "calendar": {"label": "Google Calendar", "href": "/settings",
                 "note": "Sync your real availability so RingBack only offers open times "
                         "and drops booked estimates onto your calendar.",
                 "aliases": ["calendar", "google calendar", "gcal", "schedule", "availability"]},
    "contacts": {"label": "your contacts", "href": "/callers",
                 "note": "Import your address book so RingBack knows a customer from a stranger.",
                 "aliases": ["contacts", "address book", "import contacts", "google contacts"]},
    "texting": {"label": "texting (Twilio)", "href": "/settings",
                "note": "Provision a number so RingBack texts missed callers for real.",
                "aliases": ["twilio", "texting", "text", "phone number", "sms", "number"]},
}


def _text(body):
    return {"type": "text", "body": body}


def _note(body, tone="info"):
    return {"type": "note", "body": body, "tone": tone}


def _link(title, href, label, note=""):
    return {"type": "link", "title": title, "href": href, "label": label, "note": note}


# --------------------------------------------------------------------------
# TOOL HANDLERS
# --------------------------------------------------------------------------
def _h_get_stats(business, args):
    bid = business["id"]
    a = db.analytics(bid, days=None)["totals"]
    leads = db.leads_with_stage(bid)
    warm = sum(1 for l in leads if l["stage"] == "warm")
    new = sum(1 for l in leads if l["stage"] == "new")
    appts = db.list_appointments(bid)
    review = db.count_pending_suggestions(bid)
    rev = (f"${a['revenue']:,}" if a.get("revenue") else "set a job value")
    card = {"type": "stat", "title": "Where things stand", "groups": [
        {"label": "Leads", "value": a["leads"], "sub": f"{new} not yet replied"},
        {"label": "Booked", "value": a["booked"], "sub": f"{a['conversion']}% conversion"},
        {"label": "Need chasing", "value": warm, "sub": "warm, awaiting you"},
        {"label": "Upcoming estimates", "value": len(appts), "sub": "on the calendar"},
        {"label": "To review", "value": review, "sub": "caller suggestions"},
        {"label": "Est. revenue", "value": rev, "sub": "booked x avg job value"},
    ]}
    return {"reply": "Here is the current picture. These are running totals to date, not a "
                     "7-day slice, since that is the number I can stand behind.",
            "cards": [card]}


def _h_list_leads(business, args):
    leads = db.leads_with_stage(business["id"])[:8]
    if not leads:
        return {"reply": "No leads yet. The moment a call is missed, the lead lands here.",
                "cards": []}
    items = [{"title": (l.get("name") or l.get("phone") or "Lead"),
              "sub": ("urgent · " if l.get("urgent") else "") + l["stage"]}
             for l in leads]
    return {"reply": f"Your {len(items)} most pressing leads, triage order.",
            "cards": [{"type": "list", "title": "Leads", "items": items}]}


def _h_list_appointments(business, args):
    appts = db.list_appointments(business["id"])[:8]
    if not appts:
        return {"reply": "No estimates are booked yet.", "cards": []}
    items = [{"title": (a.get("lead_name") or a.get("lead_phone") or "Estimate"),
              "sub": (a.get("scheduled_for") or a.get("slot_label") or "booked")}
             for a in appts]
    return {"reply": f"You have {len(appts)} booked estimate(s).",
            "cards": [{"type": "list", "title": "Booked estimates", "items": items}]}


def _h_add_contact(business, args):
    number = (args.get("phone") or "").strip()
    name = (args.get("name") or "").strip()
    category = (args.get("category") or "customer").strip().lower()
    if not number:
        return {"reply": "Give me a phone number and I will save them to your directory.",
                "cards": []}
    key = db.set_contact(business["id"], number, category, name=name or None)
    if not key:
        return {"reply": "That did not look like a valid phone number. Try a 10-digit number.",
                "cards": []}
    who = name or number
    return {"reply": f"Saved {who} as a {category}.",
            "cards": [_note(f"{who} is in your directory now. RingBack will treat their "
                            "calls accordingly.", "ok")]}


def _h_connect(business, args):
    prov = (args.get("provider") or "").strip().lower()
    if prov not in _CONNECT:
        prov = _match_provider(prov) or _match_provider(args.get("raw", ""))
    if not prov:
        return {"reply": "I can connect your Google Calendar, your contacts, or texting "
                         "(Twilio). Which one?",
                "cards": [_link("Open settings", "/settings", "Open settings")]}
    c = _CONNECT[prov]
    return {"reply": f"Let's connect {c['label']}. Open it and I will walk you through it.",
            "cards": [_link(f"Connect {c['label']}", c["href"], f"Connect {c['label']}",
                            note=c["note"])]}


def _h_import_contacts(business, args):
    return {"reply": "Bring your address book in from the caller inbox and I will sort who "
                     "is a customer from who is a stranger.",
            "cards": [_link("Import contacts", "/callers", "Open the caller inbox")]}


def _h_text_lead(business, args):
    """CONFIRM tool. Text a lead a short message through the gated messaging seam (opt-outs
    and simulated-vs-live honored). Resolves the named lead, else the most recent one."""
    bid = business["id"]
    leads = db.leads_with_stage(bid)
    if not leads:
        return {"reply": "There are no leads to text yet.", "cards": []}
    name = (args.get("name") or "").strip().lower()
    target = None
    if name:
        for l in leads:
            if name in (l.get("name") or "").lower() or name in (l.get("phone") or ""):
                target = l
                break
    if not target:
        target = max(leads, key=lambda l: l["id"])   # most recent
    body = (args.get("message") or "").strip()
    if not body:
        return {"reply": f"What should I text {target.get('name') or target.get('phone')}?",
                "cards": []}
    res = messaging.send_sms(business, target.get("phone", ""), body, lead_id=target["id"])
    status = res.get("status")
    msg = {"sent": f"Texted {target.get('name') or target.get('phone')}.",
           "simulated": f"Prepared the text to {target.get('name') or target.get('phone')} "
                        "(simulated until Twilio is connected).",
           "suppressed": "That contact has opted out, so nothing was sent.",
           "skipped": "I could not send that (no usable number or empty message).",
           "error": "Twilio rejected the send, so nothing went out."}.get(status, "Done.")
    tone = "ok" if status in ("sent", "simulated") else "warn"
    return {"reply": msg, "cards": [_note(msg, tone)]}


_DAYNUM = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}


def _days_label(working):
    days = sorted(working)
    if days == [0, 1, 2, 3, 4, 5, 6]:
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "Mon to Fri"
    if days == [0, 1, 2, 3, 4, 5]:
        return "Mon to Sat"
    return ", ".join(_DAYNUM[d] for d in days) or "no days set"


def _buf_label(mins):
    if not mins:
        return "no buffer"
    if mins % 60 == 0:
        h = mins // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{mins} minutes"


def _h_scheduling(business, args):
    """Read tool: show the owner's current scheduling and how to change it."""
    p = db.scheduling_prefs(business["id"])
    items = [
        {"title": "Estimate windows", "sub": ", ".join(p["times"])},
        {"title": "Working days", "sub": _days_label(p["working_days"])},
        {"title": "Buffer between estimates", "sub": _buf_label(p["buffer_minutes"])},
    ]
    return {"reply": "Here is how your scheduling is set. Tell me things like \"put a 90 "
                     "minute buffer between estimates,\" \"offer estimates at 10am and 2pm,\" "
                     "or \"only book Monday through Friday,\" and I will update it.",
            "cards": [{"type": "list", "title": "Your scheduling", "items": items},
                      _link("Open scheduling settings", "/settings", "Edit in Settings")]}


def _h_set_scheduling(business, args):
    """CONFIRM tool: apply scheduling changes (buffer / windows / working days)."""
    bid = business["id"]
    changed = []
    buf = args.get("buffer_minutes")
    times = args.get("times")
    days = args.get("working_days")
    if buf is not None:
        db.set_scheduling_prefs(bid, buffer_minutes=buf)
        changed.append(f"buffer set to {_buf_label(int(buf))}")
    if times:
        db.set_scheduling_prefs(bid, times=times)
        changed.append("estimate windows updated to " + ", ".join(times))
    if days:
        db.set_scheduling_prefs(bid, working_days=days)
        changed.append("working days set to "
                       + _days_label({int(d) for d in days}))
    if not changed:
        return _h_scheduling(business, args)
    p = db.scheduling_prefs(bid)
    return {"reply": "Done. " + "; ".join(changed) + ".",
            "cards": [_note("Updated. The AI will offer "
                            + ", ".join(p["times"]) + " on " + _days_label(p["working_days"])
                            + (", never booking two estimates within "
                               + _buf_label(p["buffer_minutes"]) + " of each other"
                               if p["buffer_minutes"] else "") + ".", "ok")]}


TOOLS = {
    "get_stats":         {"fn": _h_get_stats, "confirm": False,
                          "desc": "Show current numbers: leads, booked estimates, conversion, revenue.",
                          "params": []},
    "list_leads":        {"fn": _h_list_leads, "confirm": False,
                          "desc": "List the leads that need attention, triage order.", "params": []},
    "list_appointments": {"fn": _h_list_appointments, "confirm": False,
                          "desc": "Show booked estimates.", "params": []},
    "add_contact":       {"fn": _h_add_contact, "confirm": False,
                          "desc": "Save a phone number to the directory (customer, prospect, vendor, personal, blocked).",
                          "params": ["name", "phone", "category"]},
    "connect":           {"fn": _h_connect, "confirm": False,
                          "desc": "Connect Google Calendar, contacts, or texting (Twilio).",
                          "params": ["provider"]},
    "import_contacts":   {"fn": _h_import_contacts, "confirm": False,
                          "desc": "Import an address book from the caller inbox.", "params": []},
    "text_lead":         {"fn": _h_text_lead, "confirm": True,
                          "desc": "Text a lead a short message.",
                          "params": ["name", "message"]},
    "scheduling":        {"fn": _h_scheduling, "confirm": False,
                          "desc": "Show the owner's scheduling: estimate windows, working days, buffer between estimates.",
                          "params": []},
    "set_scheduling":    {"fn": _h_set_scheduling, "confirm": True,
                          "desc": "Change scheduling: buffer_minutes (min gap between estimates), times (estimate windows), working_days (weekday ints 0=Mon..6=Sun).",
                          "params": ["buffer_minutes", "times", "working_days"]},
}


def _confirm_summary(tool, args):
    """One human sentence describing a gated action before it runs."""
    if tool == "set_scheduling":
        parts = []
        if args.get("buffer_minutes") is not None:
            parts.append(f"keep estimates at least {_buf_label(int(args['buffer_minutes']))} apart")
        if args.get("times"):
            parts.append("offer estimates at " + ", ".join(args["times"]))
        if args.get("working_days"):
            parts.append("only book on " + _days_label({int(d) for d in args["working_days"]}))
        return ("Update your scheduling to " + "; ".join(parts) + ".") if parts \
            else "Update your scheduling."
    return {
        "text_lead": "Send this text to the lead, through the gated messaging seam.",
    }.get(tool, "Run this action.")


# --------------------------------------------------------------------------
# MATCHERS
# --------------------------------------------------------------------------
def _match_provider(text):
    t = (text or "").lower()
    for prov, c in _CONNECT.items():
        if any(a in t for a in c["aliases"]):
            return prov
    return None


_PHONE_RE = re.compile(r"(\+?\d[\d\-\.\s\(\)]{6,}\d)")

_DAYNAME = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
            "wed": 2, "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4,
            "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_TIME_RE = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?m\.?", re.I)


def _parse_buffer(t):
    """Minutes of buffer from natural language, or None. '2 hours'->120, '90 min'->90."""
    m = re.search(r"(\d+)\s*(?:hour|hr|h)\b", t)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*(?:minute|min|m)\b", t)
    if m:
        return int(m.group(1))
    return None


def _parse_times(t):
    """Estimate windows mentioned, as canonical labels ('10:00 AM'), or None."""
    out = []
    for m in _TIME_RE.finditer(t):
        h, mm = m.group(1), (m.group(2) or "00")
        out.append(f"{int(h)}:{mm} {'AM' if m.group(3).lower() == 'a' else 'PM'}")
    return out or None


def _parse_working_days(t):
    """A weekday-int list from natural language, or None."""
    t = t.lower()
    if "weekday" in t or "mon-fri" in t or "monday to friday" in t or "monday through friday" in t \
            or "no weekend" in t or "without weekend" in t:
        return [0, 1, 2, 3, 4]
    if "every day" in t or "all week" in t or "seven day" in t or "7 day" in t:
        return [0, 1, 2, 3, 4, 5, 6]
    found = sorted({v for k, v in _DAYNAME.items() if re.search(r"\b" + k + r"\b", t)})
    return found or None


# --------------------------------------------------------------------------
# THE BRAIN  (LLM tool-routing with a deterministic floor)
# --------------------------------------------------------------------------
def _tool_catalog():
    lines = []
    for name, spec in TOOLS.items():
        p = (" params: " + ", ".join(spec["params"])) if spec["params"] else ""
        lines.append(f"- {name}: {spec['desc']}{p}")
    return "\n".join(lines)


def _learning_examples(business):
    """Few-shot lines from this tenant's confirmed corrections (hook wired by the app)."""
    fn = globals().get("_learning_examples_hook")
    try:
        return fn(business["id"]) if (fn and business) else ""
    except Exception:
        return ""


def _route_system(business=None):
    taught = _learning_examples(business) if business else ""
    taught_block = ("\nThe owner has TAUGHT you these corrections; honor them:\n" + taught
                    + "\n") if taught else ""
    return (
        "You are the control assistant inside RingBack, an app that catches a home-services "
        "contractor's missed calls and books estimates by text. Decide which ONE tool best "
        "answers the owner's message and extract its parameters.\n\n"
        "TOOLS:\n" + _tool_catalog() + "\n" + taught_block + "\n"
        "Respond with ONLY a JSON object, no prose, no code fences:\n"
        '{\"tool\": \"<tool name or chat>\", \"args\": {<params>}, \"reply\": \"<one short '
        'friendly sentence>\"}\n'
        "Scheduling guidance: if the owner describes wanting estimates spaced out or not "
        "back to back (for example, not a 2pm and a 3pm), choose set_scheduling and put a "
        "sensible buffer_minutes (90 if unsure). If they name the windows or days they work, "
        "set times or working_days. Only choose scheduling (the read tool) when they ask to "
        "see or 'customize' it without a specific change.\n"
        "Use \"chat\" when they are just talking or asking something no tool covers. Never "
        "invent data. Do not use dashes; use periods and commas.")


def _llm_route(business, message, history):
    provider = ai._active_provider()
    if provider not in ("claude", "minimax"):
        return None
    convo = ""
    for turn in (history or [])[-6:]:
        who = "Owner" if turn.get("role") == "user" else "RingBack"
        convo += f"{who}: {turn.get('content', '')}\n"
    user_text = f"{convo}Owner: {message}\n\nReturn the JSON now."
    try:
        raw = ai._strip_think(ai._llm_complete(provider, _route_system(business), user_text))
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        if isinstance(data, dict) and data.get("tool"):
            return data
    except Exception as e:
        print(f"[ringback] assistant route failed, using keyword floor: {e}", flush=True)
    return None


def _demo_route(message):
    t = message.lower().strip()
    args = {"raw": message}
    if any(k in t for k in ("how many", "stats", "numbers", "how are we", "conversion",
                            "revenue", "this week", "booked how", "leads this")):
        if "list" in t and "lead" in t:
            return "list_leads", args
        return "get_stats", args

    # --- Scheduling (must run before the appointments branch: "estimate"/"buffer" overlap) ---
    times_found = _parse_times(message)
    buffer_intent = (any(k in t for k in (
        "buffer", "back to back", "back-to-back", "too close", "that close",
        "close together", "spread out", "gap between", "space between", "space out"))
        or (times_found and len(times_found) >= 2
            and any(k in t for k in ("both", "never", "make", "close", "don't want", "dont want"))))
    if buffer_intent:
        args["buffer_minutes"] = _parse_buffer(t) or 90
        return "set_scheduling", args
    wd = _parse_working_days(t)
    if wd and any(k in t for k in ("book", "schedul", "estimate", "work", "available",
                                   "only", "day")):
        args["working_days"] = wd
        return "set_scheduling", args
    if times_found and any(k in t for k in ("offer", "estimate", "available", "book at",
                                            "window", "slot", "my times", "set my")):
        args["times"] = times_found
        return "set_scheduling", args
    if any(k in t for k in ("schedul", "availability", "my hours", "my windows",
                            "what times", "what days", "customize my")):
        return "scheduling", args

    if any(k in t for k in ("appointment", "estimate", "booked", "calendar today",
                            "what's booked", "schedule")) and "connect" not in t:
        return "list_appointments", args
    if any(k in t for k in ("connect", "link", "hook up", "sync", "integrat")):
        args["provider"] = _match_provider(t) or ""
        return "connect", args
    if "import" in t and "contact" in t:
        return "import_contacts", args
    if any(k in t for k in ("text", "message", "reply to", "follow up", "follow-up")) and (
            "lead" in t or "them" in t or "him" in t or "her" in t or "back" in t):
        m = re.search(r"(?:saying|say|that|:)\s+(.*)$", message, flags=re.I)
        if m:
            args["message"] = m.group(1).strip()
        nm = re.search(r"text\s+([a-z]+)", t)
        if nm and nm.group(1) not in ("my", "the", "a", "this", "last", "them", "back"):
            args["name"] = nm.group(1)
        return "text_lead", args
    if any(k in t for k in ("save", "store", "add")) and (_PHONE_RE.search(message) or "contact" in t or "number" in t):
        ph = _PHONE_RE.search(message)
        if ph:
            args["phone"] = ph.group(1).strip()
        head = re.split(r"(\+?\d)", message)[0]
        args["name"] = re.sub(r"^(save|store|add|new)\s+(contact\s+|number\s+)?", "", head,
                              flags=re.I).strip(" ,")
        for cat in ("customer", "prospect", "vendor", "personal", "blocked"):
            if cat in t:
                args["category"] = cat
        return "add_contact", args
    if "lead" in t and ("list" in t or "show" in t or "who" in t):
        return "list_leads", args
    return "chat", args


def _route_topic(message):
    """Capability honesty: for a request with no direct tool, route to the nearest real
    page instead of dead-ending it as a 'feature request'. Returns {reply, cards} or None."""
    t = message.lower()
    if any(k in t for k in ("password", "my account", "profile", "ai instruction", "what it says",
                            "my hours", "service area", "business name", "change my", "alert",
                            "reminder")):
        return {"reply": "You can change that in Settings, including your AI instructions, hours, "
                         "alerts, and reminders.",
                "cards": [_link("Open settings", "/settings", "Open settings")]}
    if any(k in t for k in ("demo", "simulator", "try it", "test it", "see it work", "show me how")):
        return {"reply": "Run a live demo: fire a missed call and watch RingBack text the caller "
                         "and book the estimate.",
                "cards": [_link("Open the simulator", "/simulator", "Open the demo")]}
    return None


def _chat_reply(message):
    provider = ai._active_provider()
    if provider in ("claude", "minimax"):
        try:
            sys = ("You are the warm, concise control assistant for RingBack, which catches a "
                   "contractor's missed calls and books estimates. Answer in 1 to 3 sentences. "
                   "No dashes; use periods and commas. If they seem to want an action you can "
                   "take (show stats, list leads or estimates, save a contact, connect an "
                   "account, text a lead, change scheduling), offer it. Never call something a "
                   "'feature request' or say it is 'for future development'. If you cannot do it "
                   "directly, point them to the right place honestly (Settings for their profile, "
                   "hours, alerts and reminders; the simulator to see it work).")
            out = ai._strip_think(ai._llm_complete(provider, sys, message))
            if out:
                return out
        except Exception:
            pass
    return ("I am your control desk. Try \"how many leads this week,\" \"show my booked "
            "estimates,\" \"put a 90 minute buffer between estimates,\" or \"connect my Google "
            "calendar.\"")


def _chat_or_route(message, llm_reply=""):
    """Chat answer, but first route known topics to a real page (capability honesty). A
    routed reply is a capability_gap (no native tool); both are logged so we can learn."""
    routed = _route_topic(message)
    if routed:
        return {"reply": routed["reply"], "cards": routed["cards"], "pending_action": None,
                "meta": {"tool": "route", "status": "capability_gap"}}
    return {"reply": llm_reply or _chat_reply(message), "cards": [], "pending_action": None,
            "meta": {"tool": "chat", "status": "chat"}}


# --------------------------------------------------------------------------
# PUBLIC ENTRY POINTS
# --------------------------------------------------------------------------
def run(business, message, history=None):
    message = (message or "").strip()
    if not message:
        return {"reply": "What can I do for you?", "cards": [], "pending_action": None,
                "meta": {"tool": None, "status": "empty"}}

    taught = _apply_learning(business, message)   # a confirmed correction beats the brain
    if taught is not None:
        return taught

    routed = _llm_route(business, message, history)
    if routed and routed.get("tool") in TOOLS:
        tool, args = routed["tool"], (routed.get("args") or {})
        args.setdefault("raw", message)
        llm_reply = routed.get("reply") or ""
    elif routed and routed.get("tool") == "chat":
        return _chat_or_route(message, routed.get("reply") or "")
    else:
        tool, args = _demo_route(message)
        llm_reply = ""

    if tool == "chat":
        return _chat_or_route(message)

    spec = TOOLS[tool]
    if spec["confirm"]:
        # text_lead needs a message before there is anything to confirm.
        if tool == "text_lead" and not (args.get("message") or "").strip():
            out = spec["fn"](business, args)
            return {"reply": out.get("reply") or llm_reply, "cards": out.get("cards", []),
                    "pending_action": None, "meta": {"tool": tool, "status": "ok"}}
        # A vague "customize my scheduling" with no concrete change -> just show it.
        if tool == "set_scheduling" and not (
                args.get("buffer_minutes") is not None or args.get("times")
                or args.get("working_days")):
            out = _h_scheduling(business, args)
            return {"reply": out["reply"], "cards": out["cards"], "pending_action": None,
                    "meta": {"tool": "scheduling", "status": "ok"}}
        summary = _confirm_summary(tool, args)
        return {"reply": "Ready when you are. Confirm below and I will take care of it.",
                "cards": [], "pending_action": {"tool": tool, "args": args, "summary": summary},
                "meta": {"tool": tool, "status": "pending"}}

    out = spec["fn"](business, args)
    cards = out.get("cards", [])
    return {"reply": out.get("reply") or llm_reply, "cards": cards, "pending_action": None,
            "meta": {"tool": tool, "status": "ok" if cards else "empty"}}


def _apply_learning(business, message):
    """Honor a tenant's confirmed correction matching this message (deterministic, before
    the brain). The lookup hook is wired by the app to convos.lookup; a {'_run_tool': name}
    directive is executed here so tool execution stays in this module (no import cycle)."""
    fn = globals().get("_learning_lookup")
    if not fn:
        return None
    hit = fn(business, message)
    if not hit:
        return None
    if "_run_tool" in hit:
        tool = hit["_run_tool"]
        spec = TOOLS.get(tool)
        if not spec:
            return None
        if spec["confirm"]:
            summary = _confirm_summary(tool, {"raw": message})
            return {"reply": "Ready when you are. Confirm below and I will take care of it.",
                    "cards": [], "pending_action": {"tool": tool, "args": {"raw": message},
                                                    "summary": summary},
                    "meta": {"tool": tool, "status": "pending"}}
        out = spec["fn"](business, {"raw": message})
        cards = out.get("cards", [])
        return {"reply": out.get("reply", ""), "cards": cards, "pending_action": None,
                "meta": {"tool": tool, "status": "learned" if cards else "empty"}}
    return hit


def execute(business, tool, args):
    spec = TOOLS.get(tool)
    if not spec:
        return {"reply": "That action is no longer available.", "cards": [],
                "meta": {"tool": tool, "status": "error"}}
    out = spec["fn"](business, args or {})
    cards = out.get("cards", [])
    return {"reply": out.get("reply", "Done."), "cards": cards,
            "meta": {"tool": tool, "status": "ok" if cards else "empty"}}


def suggestions():
    return [
        "How many leads came in this week?",
        "Show my booked estimates",
        "Connect my Google calendar",
        "Save a number as a customer",
        "Who do I still need to chase?",
    ]
